"""Creating kiosks and changing what they are.

Onboarding *stages* live in onboarding.py. This module owns identity: the name,
where it is, whether it is active, and which of the three business
relationships it represents.

Changing `kiosk_type` is a money operation, not a label edit -- it decides whose
Razorpay account collects from students. See change_type.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskPaper

# Types where the shop's own Razorpay collects, so the shop's keys and
# subscription must be verified before it can go live.
OWNER_GATEWAY_TYPES = frozenset({KioskType.SOLD, KioskType.SAAS})


def by_legacy_id(db: Session, legacy_id: int) -> Kiosk | None:
    """The kiosk that replaced this one in the backend being replaced, if any.

    None is a real answer for a kiosk created in the admin console after
    cutover -- it replaces nothing. It is also what makes an unmapped legacy
    kiosk detectable, which the migration turns into a failed run rather than a
    quietly smaller revenue total.
    """
    return db.execute(
        select(Kiosk).where(Kiosk.legacy_id == legacy_id)
    ).scalar_one_or_none()


def create_kiosk(
    db: Session,
    *,
    name: str,
    kiosk_type: KioskType = KioskType.PLATFORM,
    location_description: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    paper_capacity: int | None = None,
    legacy_id: int | None = None,
) -> Kiosk:
    """Register a kiosk. It starts REGISTERED and is not yet listed to students.

    `legacy_id` names the kiosk in the backend being replaced, and is set only by
    the migration. Kiosks are **created through here** rather than copied row for
    row, so every rule above applies to them too; what the migration needs back
    is the link, because legacy orders and payments carry the old kiosk id and
    without a mapping their history would land nowhere.

    One old kiosk becomes one new kiosk, enforced. Two claiming the same origin
    would split that shop's history in a way no reconciliation could tell apart
    from rows that were simply lost.
    """
    name = name.strip()
    if not name:
        raise BadRequest("A kiosk needs a name.")

    if db.query(Kiosk).filter(Kiosk.name == name).first() is not None:
        raise Conflict(f"A kiosk called {name!r} already exists.")

    if legacy_id is not None and by_legacy_id(db, legacy_id) is not None:
        raise Conflict(f"Legacy kiosk {legacy_id} has already been imported.")

    kiosk = Kiosk(
        name=name,
        kiosk_type=kiosk_type,
        location_description=location_description,
        latitude=latitude,
        longitude=longitude,
        legacy_id=legacy_id,
    )
    db.add(kiosk)
    db.flush()

    # Every kiosk gets a paper row up front, so nothing downstream has to guess
    # whether one exists before reading how much paper is left.
    paper = KioskPaper(kiosk_id=kiosk.id)
    if paper_capacity is not None:
        if paper_capacity < 1:
            raise BadRequest("A paper tray holds at least one sheet.")
        paper.capacity = paper_capacity
    db.add(paper)

    return kiosk


def rename_kiosk(db: Session, kiosk: Kiosk, name: str) -> Kiosk:
    name = name.strip()
    if not name:
        raise BadRequest("A kiosk needs a name.")

    clash = db.query(Kiosk).filter(Kiosk.name == name, Kiosk.id != kiosk.id).first()
    if clash is not None:
        raise Conflict(f"A kiosk called {name!r} already exists.")

    kiosk.name = name
    db.add(kiosk)
    return kiosk


def set_location(
    db: Session,
    kiosk: Kiosk,
    *,
    description: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> Kiosk:
    """Move a kiosk on the map.

    Latitude and longitude go together: a kiosk with one and not the other
    cannot be placed, and would silently drop out of any "near me" listing.
    """
    if (latitude is None) != (longitude is None):
        raise BadRequest("Give both latitude and longitude, or neither.")

    if latitude is not None and not (-90 <= latitude <= 90):
        raise BadRequest("Latitude must be between -90 and 90.")
    if longitude is not None and not (-180 <= longitude <= 180):
        raise BadRequest("Longitude must be between -180 and 180.")

    if description is not None:
        kiosk.location_description = description
    if latitude is not None:
        kiosk.latitude = latitude
        kiosk.longitude = longitude

    db.add(kiosk)
    return kiosk


def change_type(db: Session, kiosk: Kiosk, kiosk_type: KioskType) -> Kiosk:
    """Change which business relationship a kiosk represents.

    This decides whose Razorpay account collects student money, so it cannot be
    treated as relabelling. Moving a LIVE kiosk *into* an owner-gateway type
    means the shop's keys and subscription have not been verified for it yet --
    the kiosk drops back to APPROVED and has to earn LIVE again through the
    normal gate. Leaving it live would route students' money to an account that
    may not exist.

    Moving to PLATFORM is safe to do in place: the platform's own keys always
    work, so nothing is left uncollected.
    """
    if kiosk.kiosk_type == kiosk_type:
        return kiosk

    becoming_owner_gateway = kiosk_type in OWNER_GATEWAY_TYPES
    was_selling = kiosk.onboarding_stage in {
        OnboardingStage.LIVE,
        OnboardingStage.MAINTENANCE,
        OnboardingStage.CONFIGURED,
    }

    kiosk.kiosk_type = kiosk_type

    if becoming_owner_gateway and was_selling:
        kiosk.onboarding_stage = OnboardingStage.APPROVED
        kiosk.onboarding_note = (
            "Type changed to "
            f"{kiosk_type.value}; the owner's payment details must be verified "
            "again before this kiosk can go live."
        )

    db.add(kiosk)
    return kiosk


def set_active(db: Session, kiosk: Kiosk, *, is_active: bool) -> Kiosk:
    """Take a kiosk out of service without retiring it.

    Distinct from RETIRED, which is final. This is the switch for a machine
    that is temporarily gone -- being repaired, or moved between sites.
    """
    kiosk.is_active = is_active
    db.add(kiosk)
    return kiosk


def set_accepts_wallet(db: Session, kiosk: Kiosk, *, accepts_wallet: bool) -> Kiosk:
    """Whether student wallet balance may be spent here.

    Top-ups land in the platform's Razorpay. Allowing wallet spend at an
    owner-gateway kiosk would mean the platform keeps the cash while the owner
    prints for free, so it is refused outright rather than left to a policy
    check somewhere else.
    """
    if accepts_wallet and kiosk.kiosk_type in OWNER_GATEWAY_TYPES:
        raise BadRequest(
            "Wallet balance is held by Printvendo, so it can only be spent at "
            "Printvendo-run kiosks. This kiosk takes payment into its owner's "
            "own account."
        )

    kiosk.accepts_wallet = accepts_wallet
    db.add(kiosk)
    return kiosk


def set_prices(
    db: Session,
    kiosk: Kiosk,
    *,
    bw_single: Decimal | None = None,
    bw_double: Decimal | None = None,
    color_single: Decimal | None = None,
    color_double: Decimal | None = None,
) -> Kiosk:
    """Set page prices in rupees.

    Plan floor/ceiling enforcement lives in pricing.py, which calls this after
    checking the band. Nothing here should be called directly by a route.
    """
    for label, value in (
        ("black and white, single-sided", bw_single),
        ("black and white, double-sided", bw_double),
        ("colour, single-sided", color_single),
        ("colour, double-sided", color_double),
    ):
        if value is not None and value < 0:
            raise BadRequest(f"The {label} price cannot be negative.")

    if bw_single is not None:
        kiosk.price_bw_single = bw_single
    if bw_double is not None:
        kiosk.price_bw_double = bw_double
    if color_single is not None:
        kiosk.price_color_single = color_single
    if color_double is not None:
        kiosk.price_color_double = color_double

    db.add(kiosk)
    return kiosk
