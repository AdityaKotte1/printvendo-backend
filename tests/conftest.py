import os
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.db import Base, get_engine

# Long enough that an ordinary reset never trips it, short enough that a stuck
# one reports rather than hangs the build.
RESET_LOCK_TIMEOUT = "5s"
RESET_ATTEMPTS = 3
RESET_RETRY_SECONDS = 0.5

DEFAULT_TEST_URL = "postgresql+psycopg://printvendo:printvendo@localhost:5432/printvendo_test"


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """URL of a live Postgres for tests.

    Tests run against real Postgres, never SQLite: the old backend used SQLite
    in dev and Postgres in production, which let dialect-specific bugs through.
    Locally this is the machine's Postgres 18 service; CI overrides it with
    TEST_DATABASE_URL pointing at a service container.
    """
    url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"Postgres is not reachable at {url}.\n{exc}")
    finally:
        engine.dispose()
    return url


def reset_public_schema(url: str) -> None:
    """Drop and recreate `public`, without deadlocking against a live connection.

    `drop schema public cascade` needs an AccessExclusiveLock on every table.
    Anything else holding a row lock at that moment deadlocks against it -- and
    something else genuinely is holding one: the migration tests run `alembic`
    in a subprocess, and its connection can outlive the process by a moment.
    The failure was intermittent and blamed whichever test ran before it.

    So: bound the wait with `lock_timeout` and retry, rather than letting
    Postgres detect a deadlock and kill one of the two. If it still cannot get
    the lock, say **who** is holding it -- a mystery deadlock in CI is worth far
    less than a message naming the connection to go and look at.

    One implementation, shared by the schema fixture and the migration tests.
    Two copies would drift, and the copy without the retry would fail rarely
    enough to be dismissed as noise.
    """
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        for attempt in range(RESET_ATTEMPTS):
            with engine.connect() as connection:
                connection.execute(text(f"set lock_timeout = '{RESET_LOCK_TIMEOUT}'"))
                try:
                    connection.execute(text("drop schema public cascade"))
                    connection.execute(text("create schema public"))
                    return
                except OperationalError:
                    if attempt == RESET_ATTEMPTS - 1:
                        pytest.fail(_who_holds_the_lock(engine))
                    time.sleep(RESET_RETRY_SECONDS)
    finally:
        engine.dispose()


def _who_holds_the_lock(engine) -> str:
    """Whatever is still connected, so a failure names a culprit."""
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "select pid, state, left(coalesce(query, ''), 120) "
                "from pg_stat_activity "
                "where datname = current_database() and pid <> pg_backend_pid()"
            )
        ).all()
    listing = "\n".join(f"  pid={pid} state={state} query={query!r}" for pid, state, query in rows)
    return (
        "Could not reset the test schema: something else is holding a lock on it.\n"
        f"Connections to this database:\n{listing or '  (none)'}"
    )


@pytest.fixture(scope="session")
def schema(postgres_url: str) -> str:
    """Build the whole schema from ORM metadata once per test session.

    Dropping first is what makes this correct rather than merely convenient:
    create_all uses checkfirst, so against an existing table it silently does
    nothing -- add a column to a model and every test keeps running against the
    old shape, failing with "column does not exist" a long way from the cause.
    """
    # Every module's models must be imported before create_all, or its tables
    # are simply absent from the metadata. Relying on a test module's own import
    # to register them works only until test collection order changes, and then
    # fails as "relation does not exist" a long way from the cause. This list
    # grows with each new module, exactly like migrations/env.py.
    import app.modules.billing.models  # noqa: F401
    import app.modules.identity.models  # noqa: F401
    import app.modules.kiosks.models  # noqa: F401
    import app.modules.payments.models  # noqa: F401
    import app.modules.printing.models  # noqa: F401

    reset_public_schema(postgres_url)
    Base.metadata.create_all(get_engine(postgres_url))
    return postgres_url


@pytest.fixture
def db_session(schema: str) -> Iterator[Session]:
    """A session on the shared schema, rolled back after each test.

    Tables come from Base.metadata rather than from running migrations, so a
    model test fails on the model rather than on a migration nobody has written
    yet. tests/test_migrations.py is what proves the two agree.

    The whole test runs inside one transaction that is rolled back at the end,
    so tests never see each other's rows and nothing needs cleaning up.
    """
    engine = get_engine(schema)

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        # A test that provokes an IntegrityError has already aborted the
        # transaction, and rolling back again warns. Only roll back if there is
        # still something to roll back.
        if transaction.is_active:
            transaction.rollback()
        connection.close()
