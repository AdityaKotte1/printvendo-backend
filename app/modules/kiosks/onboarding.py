"""Moving a kiosk through onboarding, and the gate that guards LIVE.

The gate is the rule that protects a shop owner from us: an owner-gateway kiosk
must not start selling until the route to the owner's own Razorpay account
exists. There are no settlements -- an owner's takings never pass through
Printvendo -- so money collected before that route exists has nowhere correct to
go.

Whether an owner can collect is a *payments* question. This module asks it
through a protocol rather than importing the payments module, for two reasons:
payments does not exist yet, and a bounded context that reaches into another's
internals is exactly what the import contracts forbid.
"""

from typing import Protocol

from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.modules.kiosks.enums import KioskType, OnboardingStage, can_transition
from app.modules.kiosks.models import Kiosk
from app.modules.kiosks.registry import OWNER_GATEWAY_TYPES

NOT_CONNECTED = (
    "This kiosk's owner has not finished connecting their Razorpay account, so "
    "their takings would arrive in Printvendo's account instead of theirs. "
    "Finish the Razorpay step, then set it live."
)


class BillingCheck(Protocol):
    """Can this kiosk's owner collect money into their own account?

    True requires both an active subscription and configured Razorpay keys.
    Implemented by the payments module; supplied by the caller until it exists.
    """

    def owner_can_collect(self, db: Session, kiosk: Kiosk) -> bool: ...


class PlatformOnlyBilling:
    """A stand-in for use before the payments module lands.

    It answers False for every owner-gateway kiosk, which fails *closed*: an
    owned kiosk cannot be set live yet. A stub that answered True would let a
    kiosk start selling with no verified route to its owner, which is the exact
    outcome the gate exists to prevent.
    """

    def owner_can_collect(self, db: Session, kiosk: Kiosk) -> bool:
        return False


def _requires_owner_billing(kiosk: Kiosk) -> bool:
    return kiosk.kiosk_type in OWNER_GATEWAY_TYPES


def move_to(
    db: Session,
    kiosk: Kiosk,
    target: OnboardingStage,
    *,
    billing: BillingCheck,
    note: str | None = None,
) -> Kiosk:
    """Move a kiosk to `target`, or refuse with a reason.

    Refuses an undefined transition, and refuses LIVE for an owner-gateway
    kiosk whose owner cannot yet collect.
    """
    current = kiosk.onboarding_stage

    if current == target:
        return kiosk

    if not can_transition(current, target):
        raise Conflict(
            f"A kiosk that is {current.value} cannot become {target.value}."
        )

    if target is OnboardingStage.LIVE and _requires_owner_billing(kiosk):
        if not billing.owner_can_collect(db, kiosk):
            raise BadRequest(NOT_CONNECTED)

    kiosk.onboarding_stage = target
    if note is not None:
        kiosk.onboarding_note = note

    db.add(kiosk)
    return kiosk


def reconcile_billing_state(db: Session, kiosk: Kiosk, *, billing: BillingCheck) -> Kiosk:
    """Keep a kiosk's stage honest as its owner's billing changes.

    The old backend only guarded the promotion to LIVE, so a kiosk whose owner
    later let their subscription lapse kept selling -- and every rupee landed in
    whichever account the routing happened to pick. Here the lapse is a state
    change: LIVE becomes SUSPENDED_BILLING, and fixing the billing puts it back.

    Platform kiosks are never suspended this way; the platform's own keys always
    work.
    """
    if not _requires_owner_billing(kiosk):
        return kiosk

    can_collect = billing.owner_can_collect(db, kiosk)

    if not can_collect and kiosk.onboarding_stage in {
        OnboardingStage.LIVE,
        OnboardingStage.MAINTENANCE,
    }:
        kiosk.onboarding_stage = OnboardingStage.SUSPENDED_BILLING
        kiosk.onboarding_note = (
            "Suspended automatically: this kiosk's owner cannot currently "
            "collect payments. It will return to service once their "
            "subscription and Razorpay keys are active again."
        )
        db.add(kiosk)

    elif can_collect and kiosk.onboarding_stage is OnboardingStage.SUSPENDED_BILLING:
        kiosk.onboarding_stage = OnboardingStage.LIVE
        kiosk.onboarding_note = None
        db.add(kiosk)

    return kiosk


def is_selling(kiosk: Kiosk) -> bool:
    """Whether a student may place an order at this kiosk right now.

    One predicate, so no caller has to remember which stages count. MAINTENANCE
    is deliberately excluded: it exists precisely so an owner can work on a
    machine without the shop looking broken.
    """
    return (
        kiosk.is_active
        and kiosk.onboarding_stage is OnboardingStage.LIVE
    )


def platform_kiosks_skip_configuration(kiosk: Kiosk) -> bool:
    """PLATFORM kiosks have nothing to configure -- our keys already exist.

    Kept as a named predicate rather than an inline comparison so the reason
    stays attached to the rule.
    """
    return kiosk.kiosk_type is KioskType.PLATFORM
