"""Finding a person, and deciding what they may do.

The last part of the platform that could only be operated in SQL. Roles are rows
rather than booleans on the user -- which is what stops a refiller being one
forgotten check away from money data -- but nothing could write those rows
except an accepted kiosk invitation, so nobody could be made an admin and nobody
could be un-made one.

Three rules here are the point of the router:

* **The search is exact, never a prefix.** A console that answers partial
  addresses is a directory of every user on the platform, walkable by whoever
  reaches it. There is no "list everyone" route for the same reason.
* **A role may be listed; STUDENT may not.** Owners, refillers and admins are
  the handful of people an operator administers -- already named on kiosks the
  same admin can list -- and requiring somebody to remember ten addresses to
  look after ten shops is not security, it is a console nobody can use.
  Students are the directory the rule above is actually about, so `role=student`
  is refused in the same sentence that says how to find one.
* **Deactivating ends access now.** `get_by_public_id` refuses an inactive
  account, so the access token dies with the row rather than fifteen minutes
  later, and `set_active` revokes the refresh family on the way out.
* **An admin cannot disarm themselves.** Revoking your own last admin role, or
  switching off your own account, leaves a platform nobody can administer --
  there is no second surface that could grant it back.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.api.schemas import (
    AccountResponse,
    CreditedWalletResponse,
    CreditWalletRequest,
)
from app.core.errors import BadRequest, NotFound
from app.core.ids import IdPrefix, new_id
from app.modules.identity import User, set_active
from app.modules.identity import repository as identity_repo
from app.modules.identity.roles import Role
from app.modules.ops import audit
from app.modules.wallet import EntryKind, balance_of, credit

router = APIRouter(prefix="/v1/admin/accounts", tags=["admin"])

CurrentAdmin = Annotated[User, Depends(require_role(Role.ADMIN))]

NO_SUCH_ACCOUNT = "That account does not exist."
NOT_YOURSELF = (
    "You cannot remove your own admin access. Ask another admin to do it, so "
    "there is always somebody who can."
)
NOT_YOUR_OWN_ACCOUNT = "You cannot deactivate your own account."

# Which roles are a set an operator manages, rather than the whole user base.
LISTABLE_ROLES = (Role.OWNER, Role.REFILLER, Role.ADMIN)

NAME_SOMEBODY = (
    "Search for an exact email address, or list a role: "
    + ", ".join(role.value for role in LISTABLE_ROLES)
    + "."
)
NO_STUDENT_DIRECTORY = (
    "Students cannot be listed -- there are too many of them for it to be "
    "anything but a directory. Search for an exact email address instead."
)


def _as_response(db: Session, user: User) -> AccountResponse:
    return AccountResponse(
        id=user.public_id,
        email=user.email,
        full_name=user.full_name,
        roles=sorted(role.value for role in identity_repo.roles_of(db, user.id)),
        is_active=user.is_active,
        is_guest=user.is_guest,
        email_verified=user.email_verified,
        created_at=user.created_at,
    )


def _account(db: Session, account_id: str) -> User:
    """The account with this id, active or not.

    Not `get_by_public_id`: that one is the authentication path and hides
    inactive accounts, which is exactly the account an admin is most likely to
    be looking for. An id of the wrong kind is 404 rather than 400 -- `ksk_...`
    here names nothing, and a more precise answer would tell a caller which
    kinds of id exist.
    """
    user = identity_repo.get_any_by_public_id(db, account_id)
    if user is None:
        raise NotFound(NO_SUCH_ACCOUNT)
    return user


@router.get("", response_model=list[AccountResponse])
def find_accounts(
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    email: Annotated[str | None, Query(min_length=3)] = None,
    role: str | None = None,
) -> list[AccountResponse]:
    """Look somebody up by their exact address, or list the people in a role.

    **By address**, the match is exact and case-insensitive. Exact because a
    console that answers prefixes is a directory of the platform; case-
    insensitive because Postgres is not, and an admin typing an address with a
    capital would otherwise be told an account does not exist -- which is how
    the legacy data's case-duplicate accounts came about. A list, because those
    duplicates exist: ten pairs in production, and hiding the second would hide
    exactly what the migration has to resolve.

    **By role**, only for the roles an operator administers. There are a few
    dozen owners and refillers and they are already named on kiosks this same
    admin can list, so making somebody remember an address per shop buys
    nothing. `student` is refused, because that is the directory the exactness
    rule exists to prevent.

    One of the two is required. Neither is not a request for everybody; it is a
    request that has not said what it wants, and it gets a sentence saying so.
    """
    if role is not None:
        return [_as_response(db, user) for user in _by_role(db, role)]

    if email is None:
        raise BadRequest(NAME_SOMEBODY)

    return [
        _as_response(db, user) for user in identity_repo.find_by_email(db, email)
    ]


def _by_role(db: Session, role: str) -> list[User]:
    """The people holding a listable role.

    An unknown role and STUDENT are refused differently on purpose: one is a
    typo, and the other is a decision somebody should be told about rather than
    left to conclude the console is broken.
    """
    try:
        wanted = Role(role)
    except ValueError:
        raise BadRequest(NAME_SOMEBODY) from None

    if wanted not in LISTABLE_ROLES:
        raise BadRequest(NO_STUDENT_DIRECTORY)

    return identity_repo.holders_of(db, wanted)


@router.get("/{account_id}", response_model=AccountResponse)
def one_account(
    account_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> AccountResponse:
    return _as_response(db, _account(db, account_id))


@router.post(
    "/{account_id}/wallet/credit",
    response_model=CreditedWalletResponse,
    status_code=status.HTTP_201_CREATED,
)
def credit_wallet(
    account_id: str,
    body: CreditWalletRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> CreditedWalletResponse:
    """Put money in an account's wallet by hand.

    This is how an admin prints without paying: credit an account, then pay
    with the balance like anybody else. The print is an ordinary paid job the
    whole way through -- one code path, no second kind of order, and nothing a
    future report has to remember to exclude.

    It is money appearing from nowhere, so two things hold it down. The entry
    kind can only be ADJUSTMENT or PROMO, never TOPUP -- a hand-made top-up is
    indistinguishable from money that actually arrived through the gateway, and
    that is the one thing this must never look like. And the note is required,
    because these entries are the line that explains why takings will not
    reconcile against Razorpay by exactly this much.
    """
    user = _account(db, account_id)

    entry = credit(
        db,
        user_id=user.id,
        amount=body.amount,
        kind=EntryKind(body.kind),
        # Unique per credit, and it says what made it. `UNIQUE (wallet_id,
        # reference)` is what stops a replayed webhook crediting twice; a
        # hand-made credit needs its own value rather than borrowing one that
        # means something else.
        reference=f"admin:{admin.public_id}:{new_id(IdPrefix.WALLET_ENTRY)}",
        note=body.note,
    )
    db.flush()

    audit.record(
        db,
        action="wallet.credited",
        entity_type="user",
        entity_id=user.public_id,
        actor_user_id=admin.id,
        after={
            "amount_inr": str(body.amount),
            "kind": body.kind,
            "note": body.note,
        },
    )

    return CreditedWalletResponse(
        account_id=user.public_id,
        balance=balance_of(db, user_id=user.id),
        entry_id=entry.public_id,
    )


@router.put("/{account_id}/roles/{role}", response_model=AccountResponse)
def grant(
    account_id: str,
    role: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> AccountResponse:
    """Give this account a role. Granting one it already holds changes nothing
    and is not an error -- two admins doing the same obvious thing is
    ordinary."""
    user = _account(db, account_id)
    granted = _role(role)

    identity_repo.grant_role(db, user.id, granted)

    audit.record(
        db,
        action="identity.role.granted",
        entity_type="user",
        entity_id=user.public_id,
        actor_user_id=admin.id,
        after={"role": granted.value},
    )
    return _as_response(db, user)


@router.delete("/{account_id}/roles/{role}", response_model=AccountResponse)
def revoke(
    account_id: str,
    role: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> AccountResponse:
    """Take a role away.

    Refused when an admin aims it at their own admin role: there is no other
    surface that can grant it back, so the last one to do that leaves a platform
    nobody can administer. Another admin may still do it, which is what keeps
    this a safety catch rather than a rule that somebody who has left the
    company keeps their access.
    """
    user = _account(db, account_id)
    revoked = _role(role)

    if revoked is Role.ADMIN and user.id == admin.id:
        raise BadRequest(NOT_YOURSELF)

    identity_repo.revoke_role(db, user.id, revoked)

    audit.record(
        db,
        action="identity.role.revoked",
        entity_type="user",
        entity_id=user.public_id,
        actor_user_id=admin.id,
        after={"role": revoked.value},
    )
    return _as_response(db, user)


@router.post("/{account_id}/deactivate", response_model=AccountResponse)
def deactivate(
    account_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> AccountResponse:
    """Stop this account, now.

    Not deletion: the row keeps its orders, payments and wallet, which other
    people's records point at. An account that can be deleted is an audit trail
    with holes in it.
    """
    user = _account(db, account_id)
    if user.id == admin.id:
        raise BadRequest(NOT_YOUR_OWN_ACCOUNT)

    set_active(db, user, is_active=False)

    audit.record(
        db,
        action="identity.account.deactivated",
        entity_type="user",
        entity_id=user.public_id,
        actor_user_id=admin.id,
        after={"is_active": False},
    )
    return _as_response(db, user)


@router.post("/{account_id}/activate", response_model=AccountResponse)
def activate(
    account_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> AccountResponse:
    """Let a deactivated account back in.

    Their old sessions stay dead -- deactivation revoked the refresh family, and
    nothing here brings it back. They sign in again, which is the correct amount
    of ceremony for an account that was switched off.
    """
    user = _account(db, account_id)

    set_active(db, user, is_active=True)

    audit.record(
        db,
        action="identity.account.activated",
        entity_type="user",
        entity_id=user.public_id,
        actor_user_id=admin.id,
        after={"is_active": True},
    )
    return _as_response(db, user)


def _role(value: str) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise BadRequest(f"{value!r} is not a role.") from None
