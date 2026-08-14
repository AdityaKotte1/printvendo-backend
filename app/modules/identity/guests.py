"""Anonymous guest accounts.

A student can print without creating an account. The guest is a real user row
holding STUDENT, flagged is_guest, with a synthetic email and a random password
nobody knows -- so it can never be signed into again, only carried by the tokens
issued at creation.

Guests have no wallet. That rule lives in the wallet module; identity only
records the flag.
"""

import secrets

from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

GUEST_EMAIL_DOMAIN = "guest.printvendo"


def create_guest(db: Session) -> User:
    user = User(
        email=f"guest_{secrets.token_hex(16)}@{GUEST_EMAIL_DOMAIN}",
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        full_name="Guest",
        is_guest=True,
    )
    db.add(user)
    db.flush()

    repo.grant_role(db, user.id, Role.STUDENT)
    return user
