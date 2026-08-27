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


class DeviceCommandKind(StrEnum):
    """Something an operator asks the machine in a shop to do.

    Named for the effect rather than for the daemon on one operating system.
    `RESTART_PRINTING` is CUPS on a Pi and the Print Spooler on Windows -- one
    request, and the machine knows which of those it has. A kind called
    `restart_cups` would be a lie on half the estate, which is the shape of
    `price_cents` holding rupees.

    There is deliberately nothing here for Ghostscript. It is not a service: a
    copy of it is started for one file and exits, so there is nothing running
    to restart, and offering the button would be offering a placebo.
    """

    RESTART_AGENT = "restart_agent"
    RESTART_PRINTING = "restart_printing"


class DeviceCommandState(StrEnum):
    """Where a command has got to.

    QUEUED and SENT are separate because they answer different questions. A
    command still QUEUED after a while means the machine is not asking -- it is
    offline. One stuck at SENT means it took the instruction and never said how
    it went, which is the machine having been restarted mid-command and is the
    ordinary way `RESTART_AGENT` ends.
    """

    QUEUED = "queued"
    SENT = "sent"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # Never picked up in time. A restart asked for an hour ago, run when the
    # machine finally reconnects, restarts something nobody is watching -- and
    # the reason it was asked for has usually resolved itself by then.
    EXPIRED = "expired"
