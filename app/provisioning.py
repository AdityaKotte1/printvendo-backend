"""Standing a kiosk up, in one call, without loosening anything.

Setting up a shop was eight requests in the right order across three surfaces.
The order is not incidental -- CONFIGURED cannot be skipped on the way to LIVE,
and a SOLD kiosk whose owner cannot collect must not reach LIVE at all -- so
doing it by hand meant knowing every rule, and getting it wrong meant a shop
that looked ready and could not sell.

This climbs the same ladder through the same services and stops exactly where
the rules say stop. What it adds is **an answer**: `blocking_reasons` says, in
sentences, what is still outstanding. An operator reading `configured` cannot
tell whether they are waiting on an owner to accept an invitation, on a
subscription, or on a set of Razorpay keys somebody has to paste in.

A use-case layer rather than a module: it spans kiosks, billing, payments and
identity, which no bounded context may do. It sits below the composition roots
so that both the admin route and the command line drive the same code -- two
implementations of "set up a shop" is precisely how one of them ends up skipping
a rung.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.modules.identity import User
from app.modules.kiosks import (
    AssignmentRole,
    BandSource,
    BillingCheck,
    Kiosk,
    KioskType,
    OnboardingStage,
    PlatformBand,
    create_kiosk,
    invite_staff,
    issue_enrolment_code,
    move_to,
    set_accepts_wallet,
    set_paper,
    set_pricing,
)
from app.modules.kiosks import repository as kiosk_repo
from app.modules.payments.configs import has_usable_keys
from app.modules.payments.gate import GateBilling, Gateway, kiosk_payment_gate

# Reasons a kiosk cannot sell yet. Sentences rather than codes, because the
# only thing that reads them is a person deciding who to chase.
NO_OWNER = (
    "This shop has no owner yet, so nobody can collect its takings. Invite one, "
    "and the shop goes live once they accept."
)
INVITE_PENDING = (
    "An invitation has been sent and is waiting to be accepted. Nothing else can "
    "be set up until somebody claims the shop."
)
NO_SUBSCRIPTION = (
    "The owner has no subscription in force. Grant a trial, or ask them to buy "
    "one, before the shop can start selling."
)
NO_KEYS = (
    "The owner has not connected their Razorpay account, so there is nowhere for "
    "their takings to go."
)


@dataclass(frozen=True)
class ProvisionedKiosk:
    """What was set up, and what is still outstanding."""

    kiosk: Kiosk
    enrolment_code: str
    # Empty when the shop is selling. Every entry is a sentence naming one thing
    # somebody has to do.
    blocked_by: list[str] = field(default_factory=list)
    # Present only when an owner was invited. The caller emails it; provisioning
    # does not send mail, for the same reason identity does not.
    owner_invite_token: str | None = None


def blocking_reasons(db: Session, kiosk: Kiosk) -> list[str]:
    """Everything standing between this kiosk and its first sale.

    Derived from the same functions the gate uses, never from a status column: a
    kiosk's stage says what somebody set, and this says whether the machinery to
    honour it currently exists.
    """
    if kiosk.kiosk_type not in (KioskType.SOLD, KioskType.SAAS):
        # A platform kiosk collects into our own account, which is always
        # configured. Nothing to wait for.
        return []

    owner = kiosk_repo.owner_of(db, kiosk)
    if owner is None:
        return [NO_OWNER]

    reasons = []
    gate = kiosk_payment_gate(db, kiosk)
    if gate is Gateway.CLOSED:
        from app.modules.billing import has_active_subscription

        if not has_active_subscription(db, owner.id):
            reasons.append(NO_SUBSCRIPTION)
        if not has_usable_keys(db, owner.id):
            reasons.append(NO_KEYS)
    return reasons


def provision_kiosk(
    db: Session,
    *,
    name: str,
    kiosk_type: KioskType,
    prices: dict[str, Decimal],
    actor_user_id: int,
    location_description: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    paper_capacity: int | None = None,
    sheets_in_tray: int | None = None,
    owner_email: str | None = None,
    bands: BandSource | None = None,
    billing: BillingCheck | None = None,
) -> ProvisionedKiosk:
    """Create a kiosk and take it as far towards selling as its type allows.

    Every step is the ordinary service call, so every rule holds: the name must
    be free, the prices must sit inside the plan's band and a double-sided sheet
    cannot cost less than a single-sided one, the ladder is climbed a rung at a
    time, and LIVE is refused for a kiosk that cannot take a payment.
    """
    billing = billing or GateBilling()
    bands = bands or PlatformBand()

    kiosk = create_kiosk(
        db,
        name=name,
        kiosk_type=kiosk_type,
        location_description=location_description,
        latitude=latitude,
        longitude=longitude,
        paper_capacity=paper_capacity,
    )

    move_to(db, kiosk, OnboardingStage.APPROVED, billing=billing)

    # No prices means the platform defaults, which `effective_prices` already
    # fills in. `set_pricing` refuses an empty change -- rightly, since a
    # request to set nothing is a mistake -- so it is not called at all rather
    # than being handed a copy of the defaults to write down.
    if prices:
        set_pricing(db, kiosk, bands=bands, **prices)

    move_to(db, kiosk, OnboardingStage.CONFIGURED, billing=billing)

    capacity = paper_capacity or kiosk.paper.capacity if kiosk.paper else paper_capacity
    set_paper(
        db,
        kiosk,
        capacity=capacity,
        sheets_left=sheets_in_tray if sheets_in_tray is not None else capacity,
        actor_user_id=actor_user_id,
        note="stocked at setup",
    )

    # Wallet where the platform collects, never where the owner does: top-ups
    # are money we hold, and spending them at a shop that keeps its own takings
    # would have us keep the cash while they print for free. `set_accepts_wallet`
    # refuses the illegal case outright, so this asks only where it is legal.
    if kiosk_type not in (KioskType.SOLD, KioskType.SAAS):
        set_accepts_wallet(db, kiosk, accepts_wallet=True)

    invite_token = None
    if owner_email:
        # Invited, never attached. Somebody must consent to owning a shop, and
        # the same machinery serves refillers.
        invite_token = invite_staff(
            db,
            kiosk,
            email=owner_email,
            role=AssignmentRole.OWNER,
            invited_by_user_id=actor_user_id,
        )

    blocked = blocking_reasons(db, kiosk)
    if invite_token is not None and blocked == [NO_OWNER]:
        # More useful than "no owner": somebody has been asked, and the shop is
        # waiting on them rather than on whoever is reading this.
        blocked = [INVITE_PENDING]

    if not blocked:
        move_to(db, kiosk, OnboardingStage.LIVE, billing=billing)

    enrolment = issue_enrolment_code(db, kiosk, created_by_user_id=actor_user_id)

    return ProvisionedKiosk(
        kiosk=kiosk,
        enrolment_code=enrolment.code,
        blocked_by=blocked,
        owner_invite_token=invite_token,
    )


def readiness(db: Session, kiosk: Kiosk) -> tuple[bool, list[str]]:
    """Whether this kiosk can sell, and why not if it cannot."""
    reasons = blocking_reasons(db, kiosk)
    selling = not reasons and kiosk.onboarding_stage is OnboardingStage.LIVE
    return selling, reasons


__all__ = [
    "ProvisionedKiosk",
    "blocking_reasons",
    "provision_kiosk",
    "readiness",
    "User",
]
