"""Session tokens: issue, rotate, detect replay, revoke.

Refresh tokens are opaque random strings stored only as SHA-256 hashes, so a
database dump cannot be used to impersonate anyone. Each login starts a family;
rotation keeps the family id, which is what lets a replay revoke the entire
chain rather than only the token presented.

The grace window is load-bearing. The backend being replaced revoked the old
token instantly on rotation, which made two concurrent refreshes race: two
tabs, or one page load firing several calls after the access token expired, and
the loser was told its token was invalid -- so the client cleared storage and
sent the user to /login. That was the "logs out frequently" bug.

Grace applies to *revocation* only. An expired token is always refused, because
expiry is the real deadline.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Unauthorized
from app.core.security import TokenType, create_token
from app.modules.identity.models import RefreshToken, User

ACCESS_LIFETIME = timedelta(minutes=15)
REFRESH_LIFETIME = timedelta(days=30)

# How long a rotated token keeps working. Long enough for a burst of parallel
# requests from several tabs, short enough to be useless to an attacker who
# steals a token and replays it later.
GRACE_SECONDS = 60

INVALID_SESSION = "Your session has expired. Please sign in again."


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_refresh(db: Session, user_id: int, family_id: str) -> str:
    token = secrets.token_urlsafe(48)
    db.add(
        RefreshToken(
            user_id=user_id,
            token_hash=_hash(token),
            family_id=family_id,
            expires_at=datetime.now(UTC) + REFRESH_LIFETIME,
        )
    )
    return token


def issue_tokens(db: Session, user: User, secret: str) -> tuple[str, str]:
    """Start a new session. Returns (access token, refresh token)."""
    family_id = secrets.token_hex(16)
    access = create_token(user.public_id, TokenType.ACCESS, secret, ACCESS_LIFETIME)
    refresh = _new_refresh(db, user.id, family_id)
    return access, refresh


def _lookup(db: Session, token: str) -> RefreshToken | None:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == _hash(token))
    return db.execute(stmt).scalar_one_or_none()


def revoke_family(db: Session, family_id: str) -> None:
    """Kill every token descended from one login, with no grace."""
    dead = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 1)
    db.query(RefreshToken).filter(RefreshToken.family_id == family_id).update(
        {"revoked_at": dead}
    )


def rotate_refresh(db: Session, token: str, secret: str) -> tuple[str, str]:
    """Exchange a refresh token for a fresh pair.

    Raises Unauthorized for an unknown, expired or replayed token.
    """
    row = _lookup(db, token)
    if row is None:
        raise Unauthorized(INVALID_SESSION)

    now = datetime.now(UTC)

    if row.expires_at <= now:
        raise Unauthorized(INVALID_SESSION)

    if row.revoked_at is not None:
        if now - row.revoked_at > timedelta(seconds=GRACE_SECONDS):
            # Presented long after it was rotated away: this token was kept,
            # which means it was stolen. Kill the whole chain.
            revoke_family(db, row.family_id)
            raise Unauthorized(INVALID_SESSION)
        # Inside the window: a concurrent refresh from another tab.

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise Unauthorized(INVALID_SESSION)

    row.revoked_at = row.revoked_at or now
    db.add(row)

    access = create_token(user.public_id, TokenType.ACCESS, secret, ACCESS_LIFETIME)
    refresh = _new_refresh(db, user.id, row.family_id)
    return access, refresh


def revoke_refresh(db: Session, token: str) -> None:
    """Sign out. No grace -- a shared machine must be signed out now."""
    row = _lookup(db, token)
    if row is None:
        return
    row.revoked_at = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 1)
    db.add(row)


def revoke_all(db: Session, user_id: int) -> None:
    """Kill every session a user has, on every device."""
    dead = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 1)
    db.query(RefreshToken).filter(RefreshToken.user_id == user_id).update(
        {"revoked_at": dead}
    )
