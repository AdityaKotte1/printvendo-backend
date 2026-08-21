"""The identity bounded context.

Import from here, never from the submodules: what this file exports is the
contract, and everything else inside the package is free to change. Nothing
outside may import identity.models -- that is enforced by .importlinter, not by
convention.
"""

from app.modules.identity.accounts import set_active
from app.modules.identity.google import sign_in_with_google
from app.modules.identity.guests import create_guest

# The entity type is part of the contract -- callers need it to annotate what
# the services hand back. Its *table* is not: importing app.modules.identity
# .models directly is a boundary violation and fails the import contracts.
from app.modules.identity.models import User
from app.modules.identity.passwords import authenticate, register
from app.modules.identity.roles import Role
from app.modules.identity.sessions import (
    issue_tokens,
    revoke_all,
    revoke_refresh,
    rotate_refresh,
)
from app.modules.identity.verification import start_verification, verify_email

__all__ = [
    "Role",
    "User",
    "authenticate",
    "create_guest",
    "issue_tokens",
    "register",
    "set_active",
    "revoke_all",
    "revoke_refresh",
    "rotate_refresh",
    "sign_in_with_google",
    "start_verification",
    "verify_email",
]
