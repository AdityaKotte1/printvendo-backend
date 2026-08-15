"""Proving that a registered address belongs to the person who typed it.

The token machinery is shared with password reset -- see tokens.py. This module
holds only what is specific to verification: what redeeming one *means*.

It does not send email. Identity has no business knowing about Brevo or SMTP,
and wiring a provider in here would make every test that registers a user depend
on it. The caller gets the token back and hands it to whatever does the sending.
"""

from sqlalchemy.orm import Session

from app.modules.identity import tokens
from app.modules.identity.models import TokenPurpose, User

TOKEN_LIFETIME = tokens.LIFETIMES[TokenPurpose.EMAIL_VERIFICATION]


def start_verification(db: Session, user: User) -> str:
    """Issue a fresh verification token, invalidating any earlier one."""
    return tokens.issue(db, user, TokenPurpose.EMAIL_VERIFICATION)


def verify_email(db: Session, token: str) -> User:
    """Redeem a token and mark the address verified."""
    user = tokens.redeem(db, token, TokenPurpose.EMAIL_VERIFICATION)
    user.email_verified = True
    db.add(user)
    return user
