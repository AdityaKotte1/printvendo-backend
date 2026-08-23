"""Putting a kiosk into the world, and moving it along.

Everything an owner or a refiller can do assumes a kiosk already exists, and
until this router there was no way to make one: the registry and the onboarding
ladder were built and tested, and nothing called them. That is the same shape as
the payment-config change request nobody could approve.

Two rules live in the services, not here, and this router is deliberately thin
over them:

* **The ladder is a table, not a sequence of ifs.** `move_to` refuses an
  undefined transition, so CONFIGURED cannot be skipped on the way to LIVE --
  the rung where an owned kiosk's Razorpay keys and subscription are confirmed.
* **Changing a kiosk's type is not relabelling.** It decides whose account
  collects, so `change_type` drops a selling kiosk back to APPROVED when it
  becomes owner-gateway.

An owner is **invited**, never assigned. The same invite machinery the owner
surface uses for refillers, answering identically whether or not the address has
an account -- an admin console is not a directory lookup.
"""

from datetime import UTC, datetime
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
    require_role,
)
from app.api.schemas import (
    AdminKioskResponse,
    CreateKioskRequest,
    InviteOwnerRequest,
    KioskLocationRequest,
    KioskTypeChangeRequest,
    PaperResponse,
    ProvisionedKioskResponse,
    ProvisionKioskRequest,
    StageChangeRequest,
)
from app.core.errors import BadRequest
from app.core.notifier import Notifier
from app.modules.identity import User
from app.modules.identity.roles import Role
from app.modules.kiosks import (
    ENROLMENT_LIFETIME,
    AssignmentRole,
    BandSource,
    BillingCheck,
    Kiosk,
    KioskType,
    OnboardingStage,
    change_type,
    create_kiosk,
    invite_staff,
    is_selling,
    move_to,
    set_location,
    sheets_remaining,
)
from app.modules.kiosks import repository as kiosk_repo
from app.modules.ops import audit
from app.provisioning import provision_kiosk, readiness

router = APIRouter(prefix="/v1/admin/kiosks", tags=["admin"])

# Every route here is admin-only, and the dependency is declared once for the
# router rather than repeated per handler -- a per-route guard is a guard
# somebody can forget to add to the next route.
CurrentAdmin = Annotated[User, Depends(require_role(Role.ADMIN))]


def _as_response(db: Session, kiosk: Kiosk) -> AdminKioskResponse:
    owner = kiosk_repo.owner_of(db, kiosk)
    return AdminKioskResponse(
        id=kiosk.public_id,
        name=kiosk.name,
        kiosk_type=KioskType(kiosk.kiosk_type).value,
        onboarding_stage=OnboardingStage(kiosk.onboarding_stage).value,
        onboarding_note=kiosk.onboarding_note,
        is_active=kiosk.is_active,
        is_selling=is_selling(kiosk),
        accepts_wallet=kiosk.accepts_wallet,
        location_description=kiosk.location_description,
        latitude=kiosk.latitude,
        longitude=kiosk.longitude,
        owner_id=owner.public_id if owner else None,
        owner_email=owner.email if owner else None,
        paper=PaperResponse(
            capacity=(kiosk.paper.capacity if kiosk.paper else 250),
            sheets_remaining=sheets_remaining(db, kiosk),
        ),
    )


def _kiosk_type(value: str) -> KioskType:
    try:
        return KioskType(value)
    except ValueError:
        raise BadRequest(f"{value!r} is not a kind of kiosk.") from None


@router.post("", response_model=AdminKioskResponse, status_code=status.HTTP_201_CREATED)
def register_kiosk(
    body: CreateKioskRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> AdminKioskResponse:
    """Register a kiosk. It starts REGISTERED and is not yet listed to students."""
    kiosk = create_kiosk(
        db,
        name=body.name,
        kiosk_type=_kiosk_type(body.kiosk_type),
        location_description=body.location_description,
        latitude=body.latitude,
        longitude=body.longitude,
        paper_capacity=body.paper_capacity,
    )

    audit.record(
        db,
        action="kiosk.created",
        entity_type="kiosk",
        entity_id=kiosk.public_id,
        actor_user_id=admin.id,
        after={"name": kiosk.name, "kiosk_type": KioskType(kiosk.kiosk_type).value},
    )
    return _as_response(db, kiosk)


@router.post(
    "/provision",
    response_model=ProvisionedKioskResponse,
    status_code=status.HTTP_201_CREATED,
)
def provision(
    body: ProvisionKioskRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    bands: Annotated[BandSource, Depends(get_band_source)],
    billing: Annotated[BillingCheck, Depends(get_billing_check)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> ProvisionedKioskResponse:
    """Stand a shop up in one request, as far as its type allows.

    Not a shortcut around the onboarding ladder -- the same services, climbed a
    rung at a time, so CONFIGURED still cannot be skipped and a SOLD kiosk whose
    owner cannot collect still cannot reach LIVE. What it adds is that the
    caller is *told* what is outstanding, in sentences, instead of inferring it
    from a stage name.
    """
    prices = {
        field: value
        for field, value in (
            ("bw_single", body.price_bw_single),
            ("bw_double", body.price_bw_double),
            ("color_single", body.price_color_single),
            ("color_double", body.price_color_double),
        )
        if value is not None
    }

    result = provision_kiosk(
        db,
        name=body.name,
        kiosk_type=_kiosk_type(body.kiosk_type),
        prices=prices,
        actor_user_id=admin.id,
        location_description=body.location_description,
        latitude=body.latitude,
        longitude=body.longitude,
        paper_capacity=body.paper_capacity,
        sheets_in_tray=body.sheets_in_tray,
        owner_email=body.owner_email,
        bands=bands,
        billing=billing,
    )

    if result.owner_invite_token is not None:
        # Provisioning does not send mail, for the same reason identity does
        # not: it would make every test that sets up a shop depend on a
        # provider. The composition root has a notifier and does it here.
        notifier.send_staff_invite(
            email=body.owner_email,
            token=result.owner_invite_token,
            kiosk_name=result.kiosk.name,
        )

    audit.record(
        db,
        action="kiosk.provisioned",
        entity_type="kiosk",
        entity_id=result.kiosk.public_id,
        actor_user_id=admin.id,
        after={
            "name": result.kiosk.name,
            "kiosk_type": KioskType(result.kiosk.kiosk_type).value,
            "stage": OnboardingStage(result.kiosk.onboarding_stage).value,
            "blocked_by": result.blocked_by,
        },
    )

    selling, _ = readiness(db, result.kiosk)
    return ProvisionedKioskResponse(
        kiosk=_as_response(db, result.kiosk),
        selling=selling,
        blocked_by=result.blocked_by,
        enrolment_code=result.enrolment_code,
        enrolment_expires_at=datetime.now(UTC) + ENROLMENT_LIFETIME,
    )


@router.get("/{kiosk_id}", response_model=AdminKioskResponse)
def one_kiosk(
    kiosk_id: str,
    admin: CurrentAdmin,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> AdminKioskResponse:
    """One kiosk, with its owner named.

    Scoped like every other kiosk read, through the same resolver -- an admin's
    scope is simply wider. There is no unscoped read anywhere, including here.
    """
    return _as_response(db, kiosk_repo.get_kiosk(db, scope, kiosk_id))


@router.post("/{kiosk_id}/stage", response_model=AdminKioskResponse)
def set_stage(
    kiosk_id: str,
    body: StageChangeRequest,
    admin: CurrentAdmin,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    billing: Annotated[BillingCheck, Depends(get_billing_check)],
) -> AdminKioskResponse:
    """Move a kiosk along the onboarding ladder.

    The whole ladder, unlike `/v1/owner/kiosks/{id}/status`, which is limited to
    LIVE and MAINTENANCE. What an admin does *not* get is a way round the rules:
    the same `move_to` refuses an undefined transition, and refuses LIVE for an
    owner-gateway kiosk whose owner cannot yet collect. Being an admin is a
    wider scope, never a bypass -- that distinction is what `/owner/*` lost in
    the backend being replaced.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)

    try:
        target = OnboardingStage(body.stage)
    except ValueError:
        raise BadRequest(f"{body.stage!r} is not a kiosk stage.") from None

    before = OnboardingStage(kiosk.onboarding_stage).value
    move_to(db, kiosk, target, billing=billing, note=body.note)

    audit.record(
        db,
        action="kiosk.stage.changed",
        entity_type="kiosk",
        entity_id=kiosk.public_id,
        actor_user_id=admin.id,
        before={"onboarding_stage": before},
        after={"onboarding_stage": OnboardingStage(kiosk.onboarding_stage).value},
        note=body.note,
    )
    return _as_response(db, kiosk)


@router.put("/{kiosk_id}/type", response_model=AdminKioskResponse)
def set_type(
    kiosk_id: str,
    body: KioskTypeChangeRequest,
    admin: CurrentAdmin,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> AdminKioskResponse:
    """Change which business relationship a kiosk represents.

    Admin-only because it decides whose Razorpay collects at that shop. An
    owner able to call this could move a platform kiosk to SOLD and point its
    takings at themselves.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    before = KioskType(kiosk.kiosk_type).value

    change_type(db, kiosk, _kiosk_type(body.type))

    audit.record(
        db,
        action="kiosk.type.changed",
        entity_type="kiosk",
        entity_id=kiosk.public_id,
        actor_user_id=admin.id,
        before={"kiosk_type": before},
        after={
            "kiosk_type": KioskType(kiosk.kiosk_type).value,
            "onboarding_stage": OnboardingStage(kiosk.onboarding_stage).value,
        },
    )
    return _as_response(db, kiosk)


@router.put("/{kiosk_id}/location", response_model=AdminKioskResponse)
def place_kiosk(
    kiosk_id: str,
    body: KioskLocationRequest,
    admin: CurrentAdmin,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> AdminKioskResponse:
    """Put a kiosk on the map, or move it.

    Coordinates could previously only be given when the kiosk was created, so a
    shop set up before anybody looked its position up stayed unplaced for ever
    -- and an unplaced shop cannot be sorted by distance or drawn on the
    student app's map, which is most of what that screen is.

    Not an owner route. Where a shop claims to be decides which students walk
    to it, and a kiosk placed on top of a busier one is a claim about somebody
    else's trade.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    before = {
        "latitude": kiosk.latitude,
        "longitude": kiosk.longitude,
        "location_description": kiosk.location_description,
    }

    set_location(
        db,
        kiosk,
        description=body.description,
        latitude=body.latitude,
        longitude=body.longitude,
    )

    audit.record(
        db,
        action="kiosk.location.set",
        entity_type="kiosk",
        entity_id=kiosk.public_id,
        actor_user_id=admin.id,
        before=before,
        after={
            "latitude": kiosk.latitude,
            "longitude": kiosk.longitude,
            "location_description": kiosk.location_description,
        },
    )
    return _as_response(db, kiosk)


@router.post("/{kiosk_id}/owner", status_code=status.HTTP_202_ACCEPTED)
def invite_owner(
    kiosk_id: str,
    body: InviteOwnerRequest,
    admin: CurrentAdmin,
    user: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> dict[str, str]:
    """Offer a kiosk to a shop. They become its owner by accepting, not by being
    told.

    The same invite machinery the owner surface uses for refillers, and the same
    identical answer either way: an admin console must not be a way to find out
    which addresses have accounts.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)

    token = invite_staff(
        db,
        kiosk,
        email=body.email,
        role=AssignmentRole.OWNER,
        invited_by_user_id=admin.id,
    )
    notifier.send_staff_invite(email=body.email, token=token, kiosk_name=kiosk.name)

    audit.record(
        db,
        action="kiosk.owner.invited",
        entity_type="kiosk",
        entity_id=kiosk.public_id,
        actor_user_id=admin.id,
        after={"email": body.email},
    )
    return {"detail": "If that address can be invited, an invitation is on its way."}
