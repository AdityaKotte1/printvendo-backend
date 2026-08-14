"""Proving that a registered address belongs to the person who typed it.

This module issues and redeems tokens. It does not send email: identity has no
business knowing about Brevo or SMTP, and wiring a provider in here would make
every test that registers a user depend on it. The caller gets the token back
and hands it to whatever does the sending.

Tokens are stored as hashes and are single-use, for the same reason refresh
tokens are: a database dump must not yield working links, and a link forwarded
or left sitting in a mailbox must not keep working.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.modules.identity.models import EmailVerification, User

TOKEN_LIFETIME = timedelta(hours=24)

INVALID_TOKEN = "That verification link is invalid or has expired. Request a new one."


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_verification(db: Session, user: User) -> str:
    """Issue a fresh verification token, invalidating any earlier one.

    Superseding matters: without it, pressing "resend" three times leaves three
    live links for one address, and revoking a leaked one means hunting them
    all down.
    """
    now = datetime.now(UTC)

    db.query(EmailVerification).filter(
        EmailVerification.user_id == user.id,
        EmailVerification.used_at.is_(None),
    ).update({"used_at": now})

    token = secrets.token_urlsafe(32)
    db.add(
        EmailVerification(
            user_id=user.id,
            token_hash=_hash(token),
            expires_at=now + TOKEN_LIFETIME,
        )
    )
    return token


def verify_email(db: Session, token: str) -> User:
    """Redeem a token and mark the address verified.

    Raises BadRequest for an unknown, expired or already-used token -- one
    message for all three, so the endpoint cannot be used to probe which tokens
    ever existed.
    """
    stmt = select(EmailVerification).where(EmailVerification.token_hash == _hash(token))
    row = db.execute(stmt).scalar_one_or_none()

    if row is None or row.used_at is not None:
        raise BadRequest(INVALID_TOKEN)

    now = datetime.now(UTC)
    if row.expires_at <= now:
        raise BadRequest(INVALID_TOKEN)

    user = db.get(User, row.user_id)
    if user is None:
        raise BadRequest(INVALID_TOKEN)

    row.used_at = now
    user.email_verified = True
    db.add_all([row, user])
    return user
