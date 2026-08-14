import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.db import Base, get_engine

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


@pytest.fixture(scope="session")
def schema(postgres_url: str) -> str:
    """Build the whole schema from ORM metadata once per test session.

    Dropping first is what makes this correct rather than merely convenient:
    create_all uses checkfirst, so against an existing table it silently does
    nothing -- add a column to a model and every test keeps running against the
    old shape, failing with "column does not exist" a long way from the cause.
    """
    import app.modules.identity.models  # noqa: F401  (register the mappers)

    engine = get_engine(postgres_url)
    with engine.begin() as connection:
        connection.execute(text("drop schema public cascade"))
        connection.execute(text("create schema public"))
    Base.metadata.create_all(engine)
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
