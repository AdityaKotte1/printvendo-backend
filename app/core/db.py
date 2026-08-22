"""Engine, session and the declarative base every module's tables hang off.

One Base and one metadata for the whole service: modules own their tables, but
they share a schema and a migration history.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, String, TypeDecorator, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)

# How long a connection attempt may take before it is a failure rather than a
# wait. Without it the driver inherits the OS TCP timeout, so a database that
# has gone away holds a request open for minutes -- and holds the health probe
# open with it, which turns "down" into "not answering".
CONNECT_TIMEOUT_SECONDS = 5


class Base(DeclarativeBase):
    pass


class EnumText(TypeDecorator):
    """Store a StrEnum as text, and load it back *as the enum*.

    Without this, a column annotated `Mapped[SomeEnum]` but typed `String`
    returns a plain str after a database round-trip. Because these are StrEnums
    the deception is quiet: `value == SomeEnum.THING` still passes, so tests go
    green, while `value.value` raises AttributeError and `value is SomeEnum.THING`
    is False. The annotation should not lie.

    Text rather than a native Postgres ENUM on purpose: adding a member to a
    native enum needs a migration and locks the type, which is a lot of ceremony
    for what is a list of strings.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[StrEnum], length: int = 32) -> None:
        self.enum_class = enum_class
        super().__init__(length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return self.enum_class(value).value

    def process_result_value(self, value: Any, dialect: Any) -> StrEnum | None:
        if value is None:
            return None
        return self.enum_class(value)


@lru_cache
def get_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def database_is_reachable(url: str) -> bool:
    """Whether a query can actually be run right now.

    Deliberately on the engine every request uses, rather than on a connection
    of its own: what a probe has to answer is "can this process serve traffic",
    and a fresh connection succeeding while the pool is exhausted or misconfigured
    would answer a different and less useful question.
    """
    try:
        with get_engine(url).connect() as connection:
            connection.execute(text("select 1"))
        return True
    except Exception:  # noqa: BLE001 - a probe reports, it does not raise
        logger.warning("health check could not reach the database", exc_info=True)
        return False


@lru_cache
def get_session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)


@contextmanager
def session_scope(url: str) -> Iterator[Session]:
    """A session that commits on success and rolls back on any exception."""
    session = get_session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def advisory_lock(connection, key: int) -> Iterator[bool]:
    """Hold a named lock for as long as the block runs, or find out somebody else has.

    What makes `--workers 4` safe for a scheduled sweep: every worker wakes on
    the same schedule and asks for the same lock, exactly one gets it, and the
    other three go back to sleep instead of running the sweep three more times.

    `try` rather than a blocking acquire, deliberately. A blocking lock would
    queue the other three and run the job four times in a row -- the behaviour
    the lock exists to prevent, arrived at slowly instead of quickly.

    Released in a finally, so a job that raises does not hold the lock until the
    connection is recycled -- which, in a pool, can be a very long time.
    """
    acquired = bool(
        connection.execute(
            text("select pg_try_advisory_lock(:key)"), {"key": key}
        ).scalar()
    )
    try:
        yield acquired
    finally:
        if acquired:
            connection.execute(text("select pg_advisory_unlock(:key)"), {"key": key})
