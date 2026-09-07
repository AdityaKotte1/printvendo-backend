"""The thing that makes a job with no caller into a job that runs.

Two properties carry this file: a job runs on its interval and not more often,
and four workers waking together run it once between them rather than four
times. Everything else here is about a failing job not taking the schedule down
with it -- which is how "nothing has run since Tuesday" happens.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.db import advisory_lock, get_engine
from app.jobs.scheduler import JOBS, Job, Scheduler

START = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def _settings(postgres_url: str) -> Settings:
    return Settings(
        ENV="dev",
        DATABASE_URL=postgres_url,
        REDIS_URL="redis://localhost:6379/0",
        JWT_SECRET_KEY="s" * 32,
        SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        CORS_ORIGINS="http://localhost:3000",
    )


def _recorder(name: str, minutes: int, key: int, ran: list[str]) -> Job:
    def run(db, settings) -> str:
        ran.append(name)
        return f"{name} ran"

    return Job(name=name, interval=timedelta(minutes=minutes), lock_key=key, run=run)


@pytest.fixture
def settings(postgres_url, schema) -> Settings:
    # `schema` so the jobs' own tables exist for the integration tests below.
    return _settings(postgres_url)


# ── when a job runs ─────────────────────────────────────────────────────────


def test_everything_runs_on_the_first_tick(settings):
    ran: list[str] = []
    scheduler = Scheduler(settings, jobs=[_recorder("a", 5, 501, ran)])

    scheduler.run_due(START)

    assert ran == ["a"]


def test_a_job_does_not_run_again_before_its_interval(settings):
    ran: list[str] = []
    scheduler = Scheduler(settings, jobs=[_recorder("a", 5, 502, ran)])

    scheduler.run_due(START)
    scheduler.run_due(START + timedelta(minutes=4))

    assert ran == ["a"]


def test_a_job_runs_again_once_its_interval_has_passed(settings):
    ran: list[str] = []
    scheduler = Scheduler(settings, jobs=[_recorder("a", 5, 503, ran)])

    scheduler.run_due(START)
    scheduler.run_due(START + timedelta(minutes=5))

    assert ran == ["a", "a"]


def test_jobs_on_different_intervals_are_independent(settings):
    ran: list[str] = []
    scheduler = Scheduler(
        settings,
        jobs=[_recorder("often", 1, 504, ran), _recorder("rarely", 60, 505, ran)],
    )

    scheduler.run_due(START)
    ran.clear()
    scheduler.run_due(START + timedelta(minutes=2))

    assert ran == ["often"]


# ── four workers, one sweep ─────────────────────────────────────────────────


def test_a_job_another_worker_is_already_running_is_skipped(settings):
    """Held from outside, which is exactly what the other three workers see."""
    ran: list[str] = []
    scheduler = Scheduler(settings, jobs=[_recorder("a", 5, 506, ran)])

    with get_engine(settings.DATABASE_URL).connect() as elsewhere:
        with advisory_lock(elsewhere, 506) as held:
            assert held is True
            scheduler.run_due(START)

    assert ran == []


def test_a_skipped_job_is_not_treated_as_having_run(settings):
    """Otherwise the worker that lost the race waits a full interval before
    trying again, and a job whose lock is briefly held is silently delayed."""
    ran: list[str] = []
    scheduler = Scheduler(settings, jobs=[_recorder("a", 5, 507, ran)])

    with get_engine(settings.DATABASE_URL).connect() as elsewhere:
        with advisory_lock(elsewhere, 507):
            scheduler.run_due(START)

    scheduler.run_due(START + timedelta(seconds=30))
    assert ran == ["a"]


def test_every_job_has_its_own_lock_key():
    keys = [job.lock_key for job in JOBS]
    assert len(set(keys)) == len(keys), "two jobs sharing a key would exclude each other"


def test_every_job_has_a_name_and_an_interval():
    assert all(job.name and job.interval > timedelta(0) for job in JOBS)


# ── failure ─────────────────────────────────────────────────────────────────


def _explodes(name: str, key: int) -> Job:
    def run(db, settings) -> str:
        raise RuntimeError("this job is broken")

    return Job(name=name, interval=timedelta(minutes=5), lock_key=key, run=run)


def test_a_failing_job_does_not_stop_the_others(settings):
    ran: list[str] = []
    scheduler = Scheduler(
        settings, jobs=[_explodes("broken", 508), _recorder("fine", 5, 509, ran)]
    )

    scheduler.run_due(START)

    assert ran == ["fine"]


def test_a_failing_job_is_tried_again_next_time_round(settings):
    scheduler = Scheduler(settings, jobs=[_explodes("broken", 510)])

    scheduler.run_due(START)
    attempted = scheduler.run_due(START + timedelta(minutes=5))

    assert "broken" in attempted


def test_a_failing_job_releases_its_lock(settings):
    scheduler = Scheduler(settings, jobs=[_explodes("broken", 511)])
    scheduler.run_due(START)

    with get_engine(settings.DATABASE_URL).connect() as connection:
        with advisory_lock(connection, 511) as acquired:
            assert acquired is True


# ── a sweep nothing runs is the defect ──────────────────────────────────────


def test_every_sweep_in_tasks_is_actually_scheduled():
    """The whole point, and the reason this test exists at all.

    `requeue_expired` was written, documented as the crash-recovery half of
    claiming, exported from its module -- and called by nothing. Every test
    around it passed, because they all called it directly. An agent that died
    mid-job therefore stranded a paid print for ever, and no test, contract or
    surface said so.

    Writing `recover_lost_tasks` reproduced the same mistake within the hour:
    deleting its entry from JOBS broke no test.

    So the rule is mechanical rather than remembered. Every function in
    `app.jobs.tasks` with a sweep's shape -- `(Session, Settings) -> str` --
    must be the `run` of some Job. Adding one and not scheduling it fails the
    build, which is the only thing that would have caught the original.
    """
    import inspect

    from app.jobs import tasks

    scheduled = {job.run for job in JOBS}

    sweeps = []
    for name, function in vars(tasks).items():
        if name.startswith("_") or not inspect.isfunction(function):
            continue
        if function.__module__ != tasks.__name__:
            continue
        parameters = list(inspect.signature(function).parameters)
        if parameters[:2] == ["db", "settings"]:
            sweeps.append((name, function))

    assert sweeps, "found no sweeps at all, so this test is checking nothing"

    unscheduled = [name for name, function in sweeps if function not in scheduled]
    assert not unscheduled, (
        "these sweeps exist and nothing runs them: " + ", ".join(sorted(unscheduled))
    )
