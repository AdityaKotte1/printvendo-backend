from collections.abc import Iterator

import pytest
from sqlalchemy import text

from app.core.db import Base, advisory_lock, get_engine, session_scope


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


def test_enum_columns_load_back_as_the_enum(db_session):
    """A `Mapped[SomeEnum]` column must not return a bare str.

    Without EnumText the annotation lies: the value round-trips as a plain
    string. Because these are StrEnums the lie is quiet -- `value == Enum.X`
    still passes, so tests stay green -- while `value.value` raises
    AttributeError and `value is Enum.X` is False. This asserts identity and
    attribute access, which are the two things equality hides.
    """
    from app.modules.kiosks.enums import AssignmentRole, KioskType, OnboardingStage
    from app.modules.kiosks.models import Kiosk

    kiosk = Kiosk(name="Enum Probe")
    db_session.add(kiosk)
    db_session.flush()
    db_session.expire(kiosk)

    assert kiosk.kiosk_type is KioskType.PLATFORM
    assert kiosk.onboarding_stage is OnboardingStage.REGISTERED
    assert kiosk.kiosk_type.value == "platform"

    assert isinstance(AssignmentRole.OWNER, AssignmentRole)


# ── the advisory lock ───────────────────────────────────────────────────────

LOCK_KEY = 918_273_001


def test_one_holder_takes_the_lock(postgres_url):
    with get_engine(postgres_url).connect() as connection:
        with advisory_lock(connection, LOCK_KEY) as acquired:
            assert acquired is True


def test_a_second_holder_is_told_no_rather_than_made_to_wait(postgres_url):
    """`try` is the whole point.

    Four workers wake on the same schedule. A blocking lock would queue all
    four, so the sweep would run four times in a row instead of once -- the
    behaviour the lock exists to prevent, arrived at slowly.
    """
    engine = get_engine(postgres_url)
    with engine.connect() as first, engine.connect() as second:
        with advisory_lock(first, LOCK_KEY) as held:
            assert held is True
            with advisory_lock(second, LOCK_KEY) as also:
                assert also is False


def test_the_lock_is_free_again_afterwards(postgres_url):
    engine = get_engine(postgres_url)
    with engine.connect() as first:
        with advisory_lock(first, LOCK_KEY):
            pass
    with engine.connect() as second:
        with advisory_lock(second, LOCK_KEY) as acquired:
            assert acquired is True


def test_a_failure_inside_the_lock_still_releases_it(postgres_url):
    """A job that raises must not take the schedule down with it for ever."""
    engine = get_engine(postgres_url)
    with engine.connect() as first:
        with pytest.raises(RuntimeError):
            with advisory_lock(first, LOCK_KEY):
                raise RuntimeError("the job failed")

    with engine.connect() as second:
        with advisory_lock(second, LOCK_KEY) as acquired:
            assert acquired is True


def test_different_keys_do_not_block_each_other(postgres_url):
    engine = get_engine(postgres_url)
    with engine.connect() as first, engine.connect() as second:
        with advisory_lock(first, LOCK_KEY), advisory_lock(second, LOCK_KEY + 1) as other:
            assert other is True
