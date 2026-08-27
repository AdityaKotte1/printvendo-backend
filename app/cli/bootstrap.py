"""Making the first admin, on a database that has none.

Every other role grant goes through `PUT /v1/admin/accounts/{id}/roles/{role}`,
which requires an admin — so on an empty database nobody can make the first one.
That is the gap this closes, and the only one.

It is deliberately narrow. **It refuses once an admin exists**, because a
command that keeps working is a way to grant admin without the audited route,
available to anyone who reaches the shell. `--force` exists for the case that
actually happens — the one admin left, or lost their password — and says out
loud that it is being used.

And it **writes to the same audit trail, under the same action name**, with no
actor. An admin who appears in the database with no trace is indistinguishable
from an intruder who granted themselves the role; "nobody did this" is a fact
worth recording, and it is not the same as failing to record who.
"""

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.modules.identity import Role, User, register
from app.modules.identity import repository as identity_repo
from app.modules.ops import audit

ALREADY_HAVE_ONE = (
    "This system already has an administrator. Grant the role through the admin "
    "console so it is recorded against whoever did it, or pass --force if you "
    "are recovering an account nobody can sign in to."
)

NEEDS_A_PASSWORD = (
    "No account exists with that address, so one has to be created and it needs "
    "a password. Pass --password, or leave it out to have one generated."
)

CANNOT_SIGN_IN = (
    "That is not an address anybody could sign in with, so the account would be "
    "unusable the moment it was made. Use a real email address."
)

# The *same* validator the login route uses, rather than a rule of our own.
# `POST /v1/app/auth/login` takes an `EmailStr`, which refuses reserved TLDs
# like `.test` -- so this command happily created `admin@printvendo.test`,
# granted it the admin role, and produced the only account on a fresh system
# together with no way to use it. Two different opinions about what an address
# is would put that trap straight back.
_ADDRESS = TypeAdapter(EmailStr)


def bootstrap_admin(
    db: Session,
    *,
    email: str,
    password: str | None,
    full_name: str | None = None,
    force: bool = False,
) -> User:
    """Make this address an administrator, creating the account if need be.

    Promotes an existing account rather than refusing it: the operator who
    registered through the app first and only then discovered they had no way in
    should not end up with a second account. Their password is untouched — this
    command grants a role, and quietly resetting a credential is a different and
    much larger thing to do.
    """
    if not force and identity_repo.anyone_holds(db, Role.ADMIN):
        raise Conflict(ALREADY_HAVE_ONE)

    try:
        _ADDRESS.validate_python(email)
    except ValidationError:
        raise BadRequest(CANNOT_SIGN_IN) from None

    user = identity_repo.get_by_email(db, email)

    if user is None:
        if not password:
            raise Conflict(NEEDS_A_PASSWORD)
        user = register(db, email, password, full_name)

    if Role.ADMIN in identity_repo.roles_of(db, user.id):
        return user

    identity_repo.grant_role(db, user.id, Role.ADMIN)
    audit.record(
        db,
        action="identity.role.granted",
        entity_type="user",
        entity_id=user.public_id,
        actor_user_id=None,
        after={"role": Role.ADMIN.value},
        note="granted from the command line, before any admin existed",
    )
    return user
