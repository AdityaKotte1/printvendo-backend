from collections.abc import Iterator

import pytest
from sqlalchemy import text

from app.core.db import Base, get_engine, session_scope


@pytest.fixture
def probe_table(postgres_url: str) -> Iterator[str]:
    """A scratch table, created and dropped around each test that needs one.

    A fixture rather than an ordered pair of tests: relying on an earlier test
    to have created the table makes the suite order-dependent, and a "cleanup
    test" that asserts nothing is not a test.
    """
    with session_scope(postgres_url) as session:
        session.execute(text("drop table if exists scope_probe"))
        session.execute(text("create table scope_probe (id int primary key)"))
    try:
        yield "scope_probe"
    finally:
        with session_scope(postgres_url) as session:
            session.execute(text("drop table if exists scope_probe"))


def _row_count(url: str) -> int:
    with session_scope(url) as session:
        return session.execute(text("select count(*) from scope_probe")).scalar_one()


def test_base_uses_a_shared_metadata():
    assert Base.metadata is not None


def test_engine_is_cached_per_url():
    url = "postgresql+psycopg://u:p@localhost:5432/pv"
    assert get_engine(url) is get_engine(url)


def test_session_scope_yields_a_working_session(postgres_url):
    with session_scope(postgres_url) as session:
        assert session.execute(text("select 1")).scalar_one() == 1


def test_session_scope_commits_on_success(postgres_url, probe_table):
    with session_scope(postgres_url) as session:
        session.execute(text("insert into scope_probe values (1)"))

    assert _row_count(postgres_url) == 1


def test_work_before_an_exception_is_discarded(postgres_url, probe_table):
    """The contract: an exception escaping the block leaves nothing behind.

    Note this holds whether the discard comes from the explicit rollback() or
    from close() tearing down the transaction — both are in session_scope, and
    the behaviour is what callers depend on. Do not read a passing test here as
    licence to delete the explicit rollback(); it states intent at the point
    where the decision is made.
    """
    with session_scope(postgres_url) as session:
        session.execute(text("insert into scope_probe values (1)"))

    with pytest.raises(RuntimeError):
        with session_scope(postgres_url) as session:
            session.execute(text("insert into scope_probe values (2)"))
            raise RuntimeError("caller blew up after writing")

    assert _row_count(postgres_url) == 1


def test_the_exception_propagates_rather_than_being_swallowed(postgres_url, probe_table):
    with pytest.raises(RuntimeError, match="caller blew up"):
        with session_scope(postgres_url) as session:
            session.execute(text("insert into scope_probe values (3)"))
            raise RuntimeError("caller blew up")
