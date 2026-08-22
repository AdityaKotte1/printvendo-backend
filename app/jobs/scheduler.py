"""Running the work nothing was calling.

`expire_stale_orders` and `purge_expired_files` were correct, tested and
unreachable: no route calls either, so unpaid orders held reserved paper for
ever and `FILE_RETENTION_DAYS = 7` was a promise nothing kept. The gap was never
in the functions.

**In the app rather than in cron.** There is no `deploy/` yet, and a job that
only runs when somebody remembers to write a crontab is the same gap in a
different file. This starts with the app, which means it also starts on the
developer's machine, which means it is exercised long before it is relied on.

**One sweep between four workers.** Every worker wakes on the same schedule and
asks Postgres for the same advisory lock; one gets it and three go back to
sleep. `pg_try_advisory_lock`, never a blocking acquire -- queueing the other
three would run the sweep four times in a row, which is the thing the lock is
for, arrived at slowly.

**A job that fails is logged and tried again.** Nothing here is allowed to end
the loop: the way "nothing has run since Tuesday" happens is one exception
escaping one tick.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import advisory_lock, get_engine, session_scope
from app.jobs import tasks

logger = logging.getLogger(__name__)

# How often the loop looks for something to do. Not how often anything runs --
# each job has its own interval, and this only bounds how late a job can be.
TICK = timedelta(seconds=30)


@dataclass(frozen=True)
class Job:
    """One thing that runs on a schedule, and the key that keeps it single."""

    name: str
    interval: timedelta
    # An arbitrary but permanent integer. Postgres advisory locks are a flat
    # namespace across the database, so these must not collide with each other
    # -- a test asserts they do not -- and must not be renumbered casually: a
    # deploy where old and new workers disagree about a job's key runs it twice.
    lock_key: int
    run: Callable[[Session, Settings], str]


JOBS: tuple[Job, ...] = (
    # Every minute, because the thing being released is reserved paper: a
    # kiosk's tray is held against an order nobody paid for, and every minute of
    # that is a minute somebody else could not print.
    Job(
        name="expire-orders",
        interval=timedelta(minutes=1),
        lock_key=8_100_001,
        run=tasks.expire_orders,
    ),
    # Hourly. FILE_RETENTION_DAYS is measured in days, so the exact minute is
    # not interesting; what matters is that it runs at all.
    Job(
        name="purge-files",
        interval=timedelta(hours=1),
        lock_key=8_100_002,
        run=tasks.purge_files,
    ),
    # A device heartbeats far more often than the five-minute window that makes
    # it late, so this only has to be frequent enough that an operator hears
    # about a dead shop while the shop is still open.
    Job(
        name="watch-offline-kiosks",
        interval=timedelta(minutes=5),
        lock_key=8_100_003,
        run=tasks.watch_offline_kiosks,
    ),
    # Paper moves at the speed of printing, and a refiller has to travel. Ten
    # minutes is well inside the time it takes to act on the alert.
    Job(
        name="watch-paper",
        interval=timedelta(minutes=10),
        lock_key=8_100_004,
        run=tasks.watch_paper,
    ),
)


class Scheduler:
    """When each job last ran, and what to do about it.

    Last-run times are per process and start empty, so every job runs once on
    the first tick after a deploy. That is deliberate for these four: each is
    idempotent, and running the retention sweep at startup is a feature.
    """

    def __init__(self, settings: Settings, jobs: Sequence[Job] = JOBS) -> None:
        self.settings = settings
        self.jobs = tuple(jobs)
        self._last_run: dict[str, datetime] = {}

    def due(self, now: datetime) -> list[Job]:
        return [
            job
            for job in self.jobs
            if job.name not in self._last_run
            or now - self._last_run[job.name] >= job.interval
        ]

    def run_due(self, now: datetime | None = None) -> list[str]:
        """Run whatever is due, and report what was attempted.

        Returns names rather than results because the caller is a loop with
        nowhere to put a result. The log is where a run is reported.
        """
        now = now or datetime.now(UTC)
        attempted = []

        for job in self.due(now):
            attempted.append(job.name)
            self._run_one(job, now)
        return attempted

    def _run_one(self, job: Job, now: datetime) -> None:
        engine = get_engine(self.settings.DATABASE_URL)
        try:
            # The lock is held on its own connection, not on the session doing
            # the work: released after that session commits, so a second worker
            # cannot take the lock and read a half-finished sweep.
            with engine.connect() as lock_connection:
                with advisory_lock(lock_connection, job.lock_key) as acquired:
                    if not acquired:
                        # Not recorded as a run. Another worker is doing it; if
                        # that worker dies mid-job, this one tries again in
                        # thirty seconds rather than in an hour.
                        logger.debug("%s is already running elsewhere", job.name)
                        return

                    with session_scope(self.settings.DATABASE_URL) as db:
                        summary = job.run(db, self.settings)

                    self._last_run[job.name] = now
                    if summary:
                        logger.info("%s: %s", job.name, summary)
        except Exception:  # noqa: BLE001 - one bad job must not end the loop
            logger.exception("%s failed", job.name)

    async def run_forever(self, *, tick: timedelta = TICK) -> None:
        """The loop, until it is cancelled at shutdown.

        Jobs are synchronous SQLAlchemy, so each tick goes to a thread: a sweep
        that took two seconds on the event loop would stall every request this
        worker is serving for two seconds.
        """
        while True:
            try:
                await asyncio.to_thread(self.run_due)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - see above
                logger.exception("the scheduler tick failed")
            await asyncio.sleep(tick.total_seconds())
