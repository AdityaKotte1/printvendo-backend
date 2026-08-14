import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _alembic(args: list[str], url: str) -> subprocess.CompletedProcess:
    # Inherit the real environment and override only DATABASE_URL. Replacing
    # os.environ wholesale breaks the subprocess on Windows, which needs PATH
    # and SYSTEMROOT to start Python at all.
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_upgrade_head_succeeds_on_an_empty_database(postgres_url):
    result = _alembic(["upgrade", "head"], postgres_url)
    assert result.returncode == 0, result.stderr


def test_autogenerate_detects_no_drift_after_upgrade(postgres_url):
    _alembic(["upgrade", "head"], postgres_url)
    result = _alembic(["check"], postgres_url)
    assert result.returncode == 0, result.stdout + result.stderr


def test_migrations_refuse_to_run_without_a_database_url():
    """A missing DATABASE_URL must fail loudly, not silently target something else.

    alembic.ini deliberately carries an empty sqlalchemy.url; without this guard
    Alembic would fall through to that empty value and produce a confusing
    driver error instead of naming the real problem.
    """
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "DATABASE_URL must be set" in result.stderr
