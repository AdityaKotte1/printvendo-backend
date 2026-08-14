"""Every query against the identity tables.

Route code never queries these tables directly; that is what keeps the storage
shape changeable and the authorisation rules in one place.

Emails are matched case-insensitively and stored as given. Postgres string
comparison is case-sensitive, so "Person@example.test" and
"person@example.test" are two different rows unless the query folds case --
which is how duplicate accounts appear.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.identity.models import User, UserRole
from app.modules.identity.roles import Role


def get_by_email(db: Session, email: str) -> User | None:
    """Active user with this email, case-insensitively."""
    stmt = select(User).where(
        func.lower(User.email) == email.strip().lower(),
        User.is_active.is_(True),
    )
    return db.execute(stmt).scalar_one_or_none()


def get_by_public_id(db: Session, public_id: str) -> User | None:
    """Active user with this public id, or None if the id is not a user id."""
    try:
        parse_id(public_id, IdPrefix.USER)
    except InvalidId:
        return None

    stmt = select(User).where(User.public_id == public_id, User.is_active.is_(True))
    return db.execute(stmt).scalar_one_or_none()


def email_exists(db: Session, email: str) -> bool:
    """Whether any row holds this email, active or not.

    Registration checks this rather than get_by_email: a deactivated account
    still owns its address, and the unique constraint would reject the insert
    anyway -- better a clear message than an integrity error.
    """
    stmt = select(User.id).where(func.lower(User.email) == email.strip().lower())
    return db.execute(stmt).first() is not None


def roles_of(db: Session, user_id: int) -> set[Role]:
    stmt = select(UserRole.role).where(UserRole.user_id == user_id)
    return {Role(r) for r in db.execute(stmt).scalars()}


def grant_role(db: Session, user_id: int, role: Role) -> None:
    """Give a user a role. Granting one they already hold is a no-op."""
    if role in roles_of(db, user_id):
        return
    db.add(UserRole(user_id=user_id, role=role))


def revoke_role(db: Session, user_id: int, role: Role) -> None:
    db.query(UserRole).filter(UserRole.user_id == user_id, UserRole.role == role).delete()
