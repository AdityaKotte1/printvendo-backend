"""Putting people on a kiosk, and taking them off.

This module exists to close a specific hole in the backend being replaced.
`POST /kiosk/printers/{id}/staff/{user_id}` checked that the caller owned the
kiosk and that the target was a refiller -- but never that the refiller had any
relationship with the caller. So an owner could:

  * enumerate the entire user table by response code (404 = no such user,
    400 = exists but not a refiller, 200 = exists and is a refiller), and
  * bind a *competitor's* refiller to their own kiosk, after which the staff
    listing returned that person's name and email.

The fix is that an owner names an address and nothing else happens. The response
is identical whether or not that address has an account, so nothing can be
learned by trying. No binding exists, and no personal detail is disclosed, until
the invitee accepts.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.modules.identity import User
from app.modules.identity import repository as identity_repo
from app.modules.kiosks.enums import AssignmentRole
from app.modules.kiosks.models import Kiosk, KioskAssignment, StaffInvite

INVITE_LIFETIME = timedelta(days=7)

INVALID_INVITE = "That invitation is invalid, has expired, or has already been used."


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invite_staff(
    db: Session,
    kiosk: Kiosk,
    *,
    email: str,
    role: AssignmentRole,
    invited_by_user_id: int,
) -> str:
    """Invite an address to work at a kiosk. Returns the token to email.

    Deliberately does not look up whether the address has an account, and
    deliberately returns the same shape either way. Checking would reintroduce
    the enumeration oracle this module exists to remove.
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        raise BadRequest("That does not look like an email address.")

    already = db.execute(
        select(KioskAssignment)
        .join(User, User.id == KioskAssignment.user_id)
        .where(
            KioskAssignment.kiosk_id == kiosk.id,
            KioskAssignment.role == role,
            User.email.ilike(email),
        )
    ).scalar_one_or_none()
    if already is not None:
        raise Conflict("That person already works at this kiosk.")

    # Supersede any earlier open invite for the same address and role, so
    # pressing "invite" twice does not leave two live links.
    now = datetime.now(UTC)
    db.query(StaffInvite).filter(
        StaffInvite.kiosk_id == kiosk.id,
        StaffInvite.email == email,
        StaffInvite.role == role,
        StaffInvite.accepted_at.is_(None),
        StaffInvite.revoked_at.is_(None),
    ).update({"revoked_at": now})

    token = secrets.token_urlsafe(32)
    db.add(
        StaffInvite(
            kiosk_id=kiosk.id,
            invited_by_user_id=invited_by_user_id,
            email=email,
            role=role,
            token_hash=_hash(token),
            expires_at=now + INVITE_LIFETIME,
        )
    )
    return token


def accept_invite(
    db: Session, token: str, *, user: User
) -> tuple[Kiosk, AssignmentRole]:
    """Redeem an invitation. The accepting user must own the invited address.

    Binding the *presenting* user rather than the invited address would turn a
    forwarded link into a way to attach an arbitrary account to someone's kiosk.

    Returns the kiosk and role rather than the assignment row, so the caller can
    answer "which kiosk, in what capacity" without reaching into the ORM.
    """
    stmt = select(StaffInvite).where(StaffInvite.token_hash == _hash(token))
    invite = db.execute(stmt).scalar_one_or_none()

    now = datetime.now(UTC)
    if (
        invite is None
        or invite.accepted_at is not None
        or invite.revoked_at is not None
        or invite.expires_at <= now
    ):
        raise BadRequest(INVALID_INVITE)

    if user.email.strip().lower() != invite.email:
        # Same message as an unknown token: telling the holder that the link is
        # real but meant for someone else discloses who was invited.
        raise BadRequest(INVALID_INVITE)

    existing = db.execute(
        select(KioskAssignment).where(
            KioskAssignment.kiosk_id == invite.kiosk_id,
            KioskAssignment.user_id == user.id,
            KioskAssignment.role == invite.role,
        )
    ).scalar_one_or_none()

    invite.accepted_at = now
    db.add(invite)

    kiosk = db.get(Kiosk, invite.kiosk_id)

    if existing is None:
        db.add(
            KioskAssignment(kiosk_id=invite.kiosk_id, user_id=user.id, role=invite.role)
        )
        db.flush()

    # The role on the account follows the assignment: someone who has accepted a
    # refiller invite is a refiller, and the identity module is where that lives.
    identity_repo.grant_role(db, user.id, _identity_role_for(invite.role))
    return kiosk, invite.role


def _identity_role_for(role: AssignmentRole):
    from app.modules.identity.roles import Role

    return Role.OWNER if role is AssignmentRole.OWNER else Role.REFILLER


def revoke_invite(db: Session, kiosk: Kiosk, *, email: str, role: AssignmentRole) -> None:
    """Withdraw an invitation that has not been accepted."""
    db.query(StaffInvite).filter(
        StaffInvite.kiosk_id == kiosk.id,
        StaffInvite.email == email.strip().lower(),
        StaffInvite.role == role,
        StaffInvite.accepted_at.is_(None),
        StaffInvite.revoked_at.is_(None),
    ).update({"revoked_at": datetime.now(UTC)})


def list_staff(db: Session, kiosk: Kiosk) -> list[tuple[User, AssignmentRole]]:
    """Everyone attached to this kiosk.

    Scoped to one kiosk by construction: the caller has already been through the
    scoped repository to get here, so this cannot leak another shop's staff.
    """
    rows = db.execute(
        select(User, KioskAssignment.role)
        .join(KioskAssignment, KioskAssignment.user_id == User.id)
        .where(KioskAssignment.kiosk_id == kiosk.id)
        .order_by(User.email)
    ).all()
    return [(user, AssignmentRole(role)) for user, role in rows]


def unassign(db: Session, kiosk: Kiosk, *, user_id: int, role: AssignmentRole) -> None:
    """Take someone off a kiosk.

    The account keeps its platform role: they may still work at another shop,
    and removing it here would silently sign them out of that one.
    """
    db.query(KioskAssignment).filter(
        KioskAssignment.kiosk_id == kiosk.id,
        KioskAssignment.user_id == user_id,
        KioskAssignment.role == role,
    ).delete()
