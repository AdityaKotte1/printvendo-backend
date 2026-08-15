"""Kiosk vocabulary, and the stages a kiosk may move between.

The backend being replaced stored onboarding_stage as a free-form String and
assigned it straight from the request body, so a typo put a kiosk into a stage
nothing recognised -- and the "which kiosks are stuck?" query then silently
treated it as stuck forever. Stages are an enum here, and the moves between them
are a table rather than scattered `if` statements.
"""

from enum import StrEnum


class KioskType(StrEnum):
    """Who installed it, whose Razorpay collects, and what Printvendo earns.

    PLATFORM: we own and run it; the print revenue is ours.
    SOLD:     the shop bought the hardware; their Razorpay; we earn subscription.
    SAAS:     the shop's own printer running our software; their Razorpay.
    """

    PLATFORM = "platform"
    SOLD = "sold"
    SAAS = "saas"


class OnboardingStage(StrEnum):
    REGISTERED = "registered"
    APPROVED = "approved"
    CONFIGURED = "configured"
    LIVE = "live"
    MAINTENANCE = "maintenance"
    SUSPENDED_BILLING = "suspended_billing"
    RETIRED = "retired"


class DeviceStatus(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"
    PRINTING = "printing"
    ERROR = "error"


class AssignmentRole(StrEnum):
    OWNER = "owner"
    REFILLER = "refiller"


# Which stages a kiosk may move to from each stage.
#
# CONFIGURED cannot be skipped on the way to LIVE: that step is where an owned
# kiosk's Razorpay keys and subscription are confirmed, and skipping it is
# exactly how a shop starts taking student money into the wrong account.
TRANSITIONS: dict[OnboardingStage, set[OnboardingStage]] = {
    OnboardingStage.REGISTERED: {OnboardingStage.APPROVED, OnboardingStage.RETIRED},
    OnboardingStage.APPROVED: {
        OnboardingStage.CONFIGURED,
        OnboardingStage.REGISTERED,
        OnboardingStage.RETIRED,
    },
    OnboardingStage.CONFIGURED: {
        OnboardingStage.LIVE,
        OnboardingStage.APPROVED,
        OnboardingStage.RETIRED,
    },
    OnboardingStage.LIVE: {
        OnboardingStage.MAINTENANCE,
        OnboardingStage.SUSPENDED_BILLING,
        OnboardingStage.RETIRED,
    },
    OnboardingStage.MAINTENANCE: {
        OnboardingStage.LIVE,
        OnboardingStage.SUSPENDED_BILLING,
        OnboardingStage.RETIRED,
    },
    # Re-entered automatically when billing lapses, and left automatically when
    # it is fixed -- see onboarding.reconcile_billing_state.
    OnboardingStage.SUSPENDED_BILLING: {
        OnboardingStage.LIVE,
        OnboardingStage.MAINTENANCE,
        OnboardingStage.RETIRED,
    },
    OnboardingStage.RETIRED: set(),
}


def can_transition(current: OnboardingStage, target: OnboardingStage) -> bool:
    return target in TRANSITIONS[current]
