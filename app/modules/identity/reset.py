"""Forgotten passwords, and changing a password you still know.

Neither existed in the backend being replaced, so a user who forgot their
password had no way back. This closes that.

Three rules here are security properties, not conveniences:

* **Requesting a reset never reveals whether the address has an account.**
  The caller gets the same answer either way; only the mailbox owner learns
  anything. Otherwise the endpoint is a free membership oracle.
* **Completing a reset signs out every session.** Someone resetting because
  they were compromised needs the attacker's refresh tokens dead, not just a
  new password on the same live sessions.
* **Changing a password requires the current one.** Without it, a stolen access
  token -- fifteen minutes of exposure -- becomes permanent account takeover.
"""

from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Unauthorized
from app.core.security import hash_password, verify_password
from app.modules.identity import repository as repo
from app.modules.identity import sessions, tokens
from app.modules.identity.models import TokenPurpose, User

TOKEN_LIFETIME = tokens.LIFETIMES[TokenPurpose.PASSWORD_RESET]

WRONG_CURRENT_PASSWORD = "That is not your current password."
SAME_PASSWORD = "Your new password must be different from your current one."


def start_reset(db: Session, email: str) -> tuple[User, str] | None:
    """Begin a reset. Returns (user, token) to email, or None if there is nobody.

    The *caller* must answer identically in both cases. Returning None rather
    than raising is deliberate: an exception invites a route that turns it into
    a distinguishable 404.

    Guests are excluded. Their address is synthetic and unreachable, so a reset
    link would go nowhere while still telling the requester the account exists.
    """
    user = repo.get_by_email(db, email)
    if user is None or user.is_guest:
        return None

    return user, tokens.issue(db, user, TokenPurpose.PASSWORD_RESET)


def complete_reset(db: Session, token: str, new_password: str) -> User:
    """Set a new password from a reset link, and end every existing session."""
    user = tokens.redeem(db, token, TokenPurpose.PASSWORD_RESET)

    user.hashed_password = hash_password(new_password)
    db.add(user)

    # The likeliest reason for a reset is that someone else has the old
    # password. Leaving their refresh tokens alive would hand them the account
    # back on the next rotation.
    sessions.revoke_all(db, user.id)

    return user


def change_password(db: Session, user: User, *, current: str, new: str) -> User:
    """Change a password while signed in.

    Requires the current password: an access token alone must not be enough to
    take over an account permanently.
    """
    if not verify_password(current, user.hashed_password):
        raise Unauthorized(WRONG_CURRENT_PASSWORD)

    if verify_password(new, user.hashed_password):
        raise BadRequest(SAME_PASSWORD)

    user.hashed_password = hash_password(new)
    db.add(user)

    # Same reasoning as a reset: a password change is how someone evicts a
    # session they no longer trust, and that only works if the old sessions die.
    # The caller is expected to issue a fresh pair for the current device.
    sessions.revoke_all(db, user.id)

    return user
