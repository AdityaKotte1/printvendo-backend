"""Registration and email/password sign-in.

Two rules here are security properties rather than conveniences:

* A wrong password and an unknown email produce the *same* error. Different
  messages let anyone enumerate which addresses have accounts.
* A successful login re-hashes when the stored hash uses a legacy scheme. Every
  user migrated from the old backend arrives with a pbkdf2_sha256 hash, and
  this is what drains them to bcrypt without a password reset.
"""

from sqlalchemy.orm import Session

from app.core.errors import Conflict, Unauthorized
from app.core.security import hash_password, needs_rehash, verify_password
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

BAD_CREDENTIALS = "That email and password do not match an account."


def register(db: Session, email: str, password: str, full_name: str | None) -> User:
    email = email.strip()
    if repo.email_exists(db, email):
        raise Conflict("An account with that email already exists.")

    user = User(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
    )
    db.add(user)
    db.flush()  # assign user.id so the role row can reference it

    repo.grant_role(db, user.id, Role.STUDENT)
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    user = repo.get_by_email(db, email)

    if user is None or not verify_password(password, user.hashed_password):
        raise Unauthorized(BAD_CREDENTIALS)

    # repo.get_by_email already filters on is_active, so this is redundant
    # today. It is here anyway because the security property should not depend
    # on a detail of another function: a later change there -- "reactivate the
    # account when they log in", say -- would otherwise turn this into an
    # authentication bypass with nothing in this file to show it. Same generic
    # message, so it cannot be used to tell a disabled account from a wrong
    # password.
    if not user.is_active:
        raise Unauthorized(BAD_CREDENTIALS)

    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(password)
        db.add(user)

    return user
