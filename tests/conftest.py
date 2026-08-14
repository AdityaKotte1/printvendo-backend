import os

import pytest
from sqlalchemy import create_engine, text

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
