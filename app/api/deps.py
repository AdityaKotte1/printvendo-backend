"""Request-scoped dependencies shared by every audience.

There is exactly one place that turns a bearer token into a user, and exactly
one that checks a role. The backend being replaced had a per-router auth
dependency, which is how /owner/* ended up admin-only with a "DO NOT LOOSEN"
comment instead of a check -- there was no single place to put the check.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import get_session_factory
from app.core.errors import Forbidden, Unauthorized
from app.core.notifier import LoggingNotifier, Notifier
from app.core.security import TokenError, TokenType, decode_token
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

NOT_SIGNED_IN = "You need to sign in to do that."
NOT_ALLOWED = "You do not have access to that."


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


def get_secret(settings: Annotated[Settings, Depends(get_settings_from_app)]) -> str:
    return settings.JWT_SECRET_KEY


def get_db(
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> Iterator[Session]:
    """One transaction per request: commit on success, roll back on any error.

    A handler that raises must not leave a half-written change behind, and
    nothing should have to remember to call commit.
    """
    session = get_session_factory(settings.DATABASE_URL)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_notifier() -> Notifier:
    """How out-of-band messages leave the system.

    Overridden in tests, and replaced by a real provider when the ops work
    lands. Defined here rather than constructed inside a handler so both
    substitutions are a one-line dependency override.
    """
    return LoggingNotifier()


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized(NOT_SIGNED_IN)
    return token


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    secret: Annotated[str, Depends(get_secret)],
) -> User:
    token = _bearer_token(request)

    try:
        claims = decode_token(token, TokenType.ACCESS, secret)
    except TokenError as exc:
        raise Unauthorized(NOT_SIGNED_IN) from exc

    # repo.get_by_public_id refuses an id of the wrong kind and any inactive
    # account, so a kiosk id in `sub` cannot resolve to a user.
    user = repo.get_by_public_id(db, claims.subject)
    if user is None:
        raise Unauthorized(NOT_SIGNED_IN)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(role: Role):
    """Dependency factory: refuse anyone who does not hold `role`."""

    def _guard(
        user: CurrentUser,
        db: Annotated[Session, Depends(get_db)],
    ) -> User:
        if role not in repo.roles_of(db, user.id):
            raise Forbidden(NOT_ALLOWED)
        return user

    return _guard
