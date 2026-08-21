"""Switching an account off, and back on.

`is_active` is already read by `get_by_public_id`, which every authenticated
request goes through -- so clearing it ends the account's access at the next
request rather than at the next token expiry. That is the whole reason
deactivation is a column read on the hot path rather than a flag consulted at
sign-in only.

Refresh tokens are revoked here too. Without that, a deactivated account keeps a
valid refresh token: reactivating would silently restore a session somebody had
been signed out of, and a stolen refresh token would outlive the account it was
taken from.
"""

from sqlalchemy.orm import Session

from app.modules.identity.models import User
from app.modules.identity.sessions import revoke_all


def set_active(db: Session, user: User, *, is_active: bool) -> User:
    """Let this account in, or stop it.

    Deactivating is not deletion: the row keeps its orders, its payments and its
    wallet, all of which other people's records point at. An account that can be
    deleted is an audit trail with holes in it.
    """
    user.is_active = is_active
    if not is_active:
        revoke_all(db, user.id)

    db.add(user)
    db.flush()
    return user
