"""One-time tokens: issue, redeem, supersede.

Email verification and password reset are the same mechanism with different
lifetimes and consequences, so they share one implementation. Two copies of
"hashed, single-use, expiring token" is two places to get the security
properties subtly wrong, and they drift -- one grows a replay guard the other
never gets.

Properties every purpose inherits:

* stored only as a SHA-256 hash, so a database dump yields no working links
* single-use, so a link forwarded or left in a mailbox stops working
* issuing supersedes any earlier open token for the same user and purpose, so
  pressing "resend" three times does not leave three live links
* one error message for unknown, expired and spent, so the endpoint cannot be
  used to probe which tokens ever existed
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.modules.identity.models import OneTimeToken, TokenPurpose, User

# Verification is a convenience; a stale link is a mild annoyance.
# A reset link is a live credential -- anyone holding it can take the account,
# so it gets an hour rather than a day.
LIFETIMES: dict[TokenPurpose, timedelta] = {
    TokenPurpose.EMAIL_VERIFICATION: timedelta(hours=24),
    TokenPurpose.PASSWORD_RESET: timedelta(hours=1),
}

INVALID = "That link is invalid or has expired. Request a new one."


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(db: Session, user: User, purpose: TokenPurpose) -> str:
    """Mint a token, invalidating any earlier open one for the same purpose."""
    now = datetime.now(UTC)

    db.query(OneTimeToken).filter(
        OneTimeToken.user_id == user.id,
        OneTimeToken.purpose == purpose,
        OneTimeToken.used_at.is_(None),
    ).update({"used_at": now})

    token = secrets.token_urlsafe(32)
    db.add(
        OneTimeToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=_hash(token),
            expires_at=now + LIFETIMES[purpose],
        )
    )
    return token


def redeem(db: Session, token: str, purpose: TokenPurpose) -> User:
    """Spend a token and return whose it was.

    Raises BadRequest for unknown, expired, already-spent, or issued for a
    different purpose -- all with the same message. A verification link must not
    be usable to reset a password, and vice versa.
    """
    stmt = select(OneTimeToken).where(OneTimeToken.token_hash == _hash(token))
    row = db.execute(stmt).scalar_one_or_none()

    now = datetime.now(UTC)
    if (
        row is None
        or row.purpose != purpose
        or row.used_at is not None
        or row.expires_at <= now
    ):
        raise BadRequest(INVALID)

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise BadRequest(INVALID)

    row.used_at = now
    db.add(row)
    return user
