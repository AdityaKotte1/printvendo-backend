"""Kiosk management for the owner app.

Every handler resolves its kiosk through the scoped repository, so an owner
asking for someone else's kiosk gets a 404 -- not a 403, which would confirm it
exists.

Admin reaches these same routes with a wider scope rather than through a
parallel router. That is the whole point: the backend being replaced had a
separate admin router that filtered by the id in the URL without checking
ownership, safe only because a dependency restricted it to admins, and carrying
a comment begging nobody to loosen it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    KioskScope,
    get_band_source,
    get_billing_check,
    get_db,
    get_notifier,
    require_any_role,
)
from app.api.schemas import (
    DeviceStatusResponse,
    EnrolmentCodeResponse,
    InviteStaffRequest,
    OwnerKioskResponse,
    PaperResponse,
    PaperUpdateRequest,
    PricingResponse,
    PricingUpdateRequest,
    RefillLogResponse,
    StaffMemberResponse,
    StatusChangeRequest,
)
from app.core.errors import BadRequest
from app.core.notifier import Notifier
from app.modules.identity.roles import Role
from app.modules.kiosks import (
    AssignmentRole,
    BandSource,
    BillingCheck,
    Kiosk,
    OnboardingStage,
    device_of,
    is_online,
    is_selling,
    issue_enrolment_code,
    move_to,
    read_pricing,
    revoke_device,
    set_pricing,
)
from app.modules.kiosks import repository as kiosk_repo
from app.modules.kiosks.paper import reset_paper, set_paper, sheets_remaining
from app.modules.kiosks.staffing import invite_staff, list_staff, unassign

router = APIRouter(
    prefix="/v1/owner/kiosks",
    tags=["owner"],
    # On the router, so a route added here cannot forget it.
    dependencies=[Depends(require_any_role(Role.OWNER, Role.ADMIN))],
)

# Stages an owner may set for themselves. ERROR and RETIRED are admin
# decisions, and the onboarding ladder is not an owner's to climb.
OWNER_SETTABLE = {OnboardingStage.LIVE, OnboardingStage.MAINTENANCE}


def _paper_of(db: Session, kiosk: Kiosk) -> PaperResponse:
    return PaperResponse(
        capacity=(kiosk.paper.capacity if kiosk.paper else 250),
        sheets_remaining=sheets_remaining(db, kiosk),
    )


def _as_response(db: Session, kiosk: Kiosk) -> OwnerKioskResponse:
    return OwnerKioskResponse(
        id=kiosk.public_id,
        name=kiosk.name,
        kiosk_type=kiosk.kiosk_type,
        onboarding_stage=kiosk.onboarding_stage,
        onboarding_note=kiosk.onboarding_note,
        is_active=kiosk.is_active,
        is_selling=is_selling(kiosk),
        accepts_wallet=kiosk.accepts_wallet,
        location_description=kiosk.location_description,
        paper=_paper_of(db, kiosk),
    )


@router.get("", response_model=list[OwnerKioskResponse])
def list_my_kiosks(
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> list[OwnerKioskResponse]:
    return [_as_response(db, k) for k in kiosk_repo.list_kiosks(db, scope)]


@router.get("/{kiosk_id}", response_model=OwnerKioskResponse)
def get_my_kiosk(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> OwnerKioskResponse:
    return _as_response(db, kiosk_repo.get_kiosk(db, scope, kiosk_id))


@router.post("/{kiosk_id}/status", response_model=OwnerKioskResponse)
def set_status(
    kiosk_id: str,
    payload: StatusChangeRequest,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    billing: Annotated[BillingCheck, Depends(get_billing_check)],
) -> OwnerKioskResponse:
    """Take a kiosk in or out of service.

    Maintenance is the state that lets an owner work on a machine without the
    shop looking broken, which is why it is theirs to set. Everything else in
    the onboarding ladder is an admin decision.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)

    try:
        target = OnboardingStage(payload.stage)
    except ValueError:
        raise BadRequest(f"{payload.stage!r} is not a kiosk status.") from None

    if target not in OWNER_SETTABLE:
        raise BadRequest(
            "You can set a kiosk live or into maintenance. Anything else is "
            "handled by the Printvendo team."
        )

    move_to(db, kiosk, target, billing=billing)
    return _as_response(db, kiosk)


@router.get("/{kiosk_id}/pricing", response_model=PricingResponse)
def get_pricing(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    bands: Annotated[BandSource, Depends(get_band_source)],
) -> PricingResponse:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    return PricingResponse(**read_pricing(db, kiosk, bands=bands))


@router.put("/{kiosk_id}/pricing", response_model=PricingResponse)
def update_pricing(
    kiosk_id: str,
    payload: PricingUpdateRequest,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    bands: Annotated[BandSource, Depends(get_band_source)],
) -> PricingResponse:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    result = set_pricing(
        db,
        kiosk,
        bands=bands,
        **payload.model_dump(exclude_none=True),
    )
    return PricingResponse(**result)


@router.get("/{kiosk_id}/paper", response_model=PaperResponse)
def get_paper(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> PaperResponse:
    return _paper_of(db, kiosk_repo.get_kiosk(db, scope, kiosk_id))


@router.put("/{kiosk_id}/paper", response_model=PaperResponse)
def update_paper(
    kiosk_id: str,
    payload: PaperUpdateRequest,
    user: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> PaperResponse:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    set_paper(
        db,
        kiosk,
        capacity=payload.capacity,
        sheets_left=payload.sheets_left,
        actor_user_id=user.id,
        note=payload.note,
    )
    return _paper_of(db, kiosk)


@router.post("/{kiosk_id}/paper/reset", response_model=PaperResponse)
def refill_paper(
    kiosk_id: str,
    user: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> PaperResponse:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    reset_paper(db, kiosk, actor_user_id=user.id)
    return _paper_of(db, kiosk)


@router.get("/{kiosk_id}/refill-logs", response_model=list[RefillLogResponse])
def refill_logs(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    limit: int = 50,
) -> list[RefillLogResponse]:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    return [
        RefillLogResponse(
            sheets_added=row.sheets_added,
            capacity_at_change=row.capacity_at_change,
            used_before_change=row.used_before_change,
            note=row.note,
            created_at=row.created_at,
        )
        for row in kiosk_repo.list_refill_logs(db, kiosk, limit=limit)
    ]


@router.get("/{kiosk_id}/staff", response_model=list[StaffMemberResponse])
def kiosk_staff(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> list[StaffMemberResponse]:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    return [
        StaffMemberResponse(
            user_id=user.public_id,
            email=user.email,
            full_name=user.full_name,
            role=role.value,
        )
        for user, role in list_staff(db, kiosk)
    ]


@router.post("/{kiosk_id}/staff/invite", status_code=status.HTTP_202_ACCEPTED)
def invite(
    kiosk_id: str,
    payload: InviteStaffRequest,
    user: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> dict[str, str]:
    """Invite someone to work at this kiosk.

    Answers the same way whether or not the address has an account -- that is
    what stops this being a directory lookup for every refiller on the platform.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)

    try:
        role = AssignmentRole(payload.role)
    except ValueError:
        raise BadRequest(f"{payload.role!r} is not a role you can invite.") from None

    token = invite_staff(
        db, kiosk, email=payload.email, role=role, invited_by_user_id=user.id
    )
    notifier.send_staff_invite(email=payload.email, token=token, kiosk_name=kiosk.name)

    return {"detail": "If that address can be invited, an invitation is on its way."}


@router.delete("/{kiosk_id}/staff/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_staff(
    kiosk_id: str,
    user_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    role: str = "refiller",
) -> None:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)

    try:
        assignment_role = AssignmentRole(role)
    except ValueError:
        raise BadRequest(f"{role!r} is not a role.") from None

    target = kiosk_repo.resolve_staff_user(db, kiosk, user_id)
    unassign(db, kiosk, user_id=target.id, role=assignment_role)


# ── the machine in the shop ─────────────────────────────────────────────────


@router.get("/{kiosk_id}/device", response_model=DeviceStatusResponse)
def device_status(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> DeviceStatusResponse:
    """What the owner needs to answer "is my printer working?".

    `online` is derived from the last heartbeat rather than read from the status
    column: a Pi whose power was pulled never gets to say it went away, and the
    old backend's status stayed ONLINE until somebody noticed.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    device = device_of(db, kiosk)

    if device is None:
        return DeviceStatusResponse(
            registered=False,
            device_key=None,
            status=None,
            agent_version=None,
            last_heartbeat_at=None,
            online=False,
        )

    return DeviceStatusResponse(
        registered=True,
        device_key=device.device_key,
        status=device.status.value,
        agent_version=device.agent_version,
        last_heartbeat_at=device.last_heartbeat_at,
        online=is_online(device),
    )


@router.post("/{kiosk_id}/device/enrol", response_model=EnrolmentCodeResponse)
def enrol_device(
    kiosk_id: str,
    user: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> EnrolmentCodeResponse:
    """Mint the one-time code an installer types into the Pi.

    Issued for one kiosk by somebody who already had the right to touch that
    kiosk, so `/v1/device/register` -- which has to be public, because a machine
    being installed has no token yet -- cannot be used to attach a machine to
    somebody else's shop.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    issued = issue_enrolment_code(db, kiosk, created_by_user_id=user.id)

    return EnrolmentCodeResponse(code=issued.code, expires_at=issued.expires_at)


@router.delete("/{kiosk_id}/device", status_code=status.HTTP_204_NO_CONTENT)
def revoke_kiosk_device(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Detach the machine. Its token stops working immediately.

    This is what an owner does when a Pi is swapped, returned or stolen -- the
    old backend had no way to do it at all.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    revoke_device(db, kiosk)
