import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests.conftest import rebuild_schema, reset_public_schema

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def empty_database(postgres_url: str) -> Iterator[str]:
    """A schema with nothing in it, for migrations to build from scratch.

    The db_session fixture builds tables with Base.metadata.create_all, and
    tests/modules runs before this file. Without the reset, `alembic upgrade
    head` meets tables that already exist and fails on the first create_table --
    a failure about test ordering, not about the migration.

    **And it is put back afterwards.** These tests leave the schema in whatever
    state they were testing -- `test_downgrade_to_base_removes_everything`
    leaves it empty by design -- while the `schema` fixture that every other
    test relies on is session-scoped and builds the tables exactly once. So
    anything running after this file found no `users` table and failed for a
    reason that had nothing to do with it. Adding a test file whose name sorts
    after `test_migrations` was enough to trip it.
    """
    reset_public_schema(postgres_url)
    try:
        yield postgres_url
    finally:
        rebuild_schema(postgres_url)


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


def test_upgrade_head_succeeds_on_an_empty_database(empty_database):
    result = _alembic(["upgrade", "head"], empty_database)
    assert result.returncode == 0, result.stderr


def test_autogenerate_detects_no_drift_after_upgrade(empty_database):
    """The migrations and the ORM models must describe the same schema.

    This is the test that catches a model changed without a migration -- the
    failure mode that turns into a production deploy where the code expects a
    column the database does not have.
    """
    upgrade = _alembic(["upgrade", "head"], empty_database)
    assert upgrade.returncode == 0, upgrade.stderr

    result = _alembic(["check"], empty_database)
    assert result.returncode == 0, result.stdout + result.stderr


def test_downgrade_to_base_removes_everything(empty_database):
    """A migration that cannot be undone cannot be rolled back in a bad deploy."""
    _alembic(["upgrade", "head"], empty_database)

    result = _alembic(["downgrade", "base"], empty_database)
    assert result.returncode == 0, result.stderr

    engine = create_engine(empty_database)
    with engine.connect() as connection:
        for table in ("users", "user_roles", "refresh_tokens"):
            exists = connection.execute(
                text("select to_regclass(:t)"), {"t": table}
            ).scalar_one()
            assert exists is None, f"{table} survived the downgrade"
    engine.dispose()


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
