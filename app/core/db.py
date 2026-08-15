"""Engine, session and the declarative base every module's tables hang off.

One Base and one metadata for the whole service: modules own their tables, but
they share a schema and a migration history.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, String, TypeDecorator, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


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
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


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
