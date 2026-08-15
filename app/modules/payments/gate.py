"""Whose Razorpay account collects at a given kiosk.

**This is the single most important function in the codebase.** At least four
things depend on the answer: who gets paid, whether wallet balance may be spent
at a kiosk, where a refund must be issued from, and whether the kiosk may be
listed to students at all.

The backend being replaced derived it in three places -- `gateway_routing.py`,
`wallet_eligibility.py`, and inline in the refund path. Two of them drifted
apart, and that is what caused the wallet money leak. So there is one function
here, every consumer calls it, and nothing re-expresses the rule.

The three answers:

  PLATFORM_GATEWAY   Printvendo's keys take the payment. The money is ours.
  OWNER_GATEWAY      The shop's own keys take it. The money never touches us.
  CLOSED             Nobody may take money here. The kiosk cannot sell.

CLOSED is not an error state -- it is the correct answer when a shop that is
supposed to collect its own takings currently cannot. Letting the platform
collect instead would mean holding a shop's money with no settlement mechanism
to return it, since owners are paid directly and settlements are retired.
"""

from enum import StrEnum

from sqlalchemy.orm import Session

from app.modules.billing.subscriptions import has_active_subscription
from app.modules.kiosks import Kiosk, KioskType
from app.modules.kiosks import repository as kiosk_repo
from app.modules.payments.configs import has_usable_keys


class Gateway(StrEnum):
    PLATFORM_GATEWAY = "platform_gateway"
    OWNER_GATEWAY = "owner_gateway"
    CLOSED = "closed"


# The kiosk types where the shop collects its own takings.
OWNER_COLLECTS = frozenset({KioskType.SOLD, KioskType.SAAS})


def kiosk_payment_gate(db: Session, kiosk: Kiosk) -> Gateway:
    """Which account collects at this kiosk right now.

    Never cached and never inferred from `kiosk_type` alone: the type says what
    the commercial relationship *is*, and this says whether the machinery to
    honour it currently exists. Production had a SOLD kiosk with no owner at
    all, whose takings were quietly landing in the platform's account.
    """
    if kiosk.kiosk_type not in OWNER_COLLECTS:
        # A platform kiosk always works -- our own keys are always configured.
        return Gateway.PLATFORM_GATEWAY

    owner = kiosk_repo.owner_of(db, kiosk)
    if owner is None:
        return Gateway.CLOSED

    if not has_active_subscription(db, owner.id):
        return Gateway.CLOSED

    if not has_usable_keys(db, owner.id):
        return Gateway.CLOSED

    return Gateway.OWNER_GATEWAY


def can_take_payment(db: Session, kiosk: Kiosk) -> bool:
    """Whether a student may pay at this kiosk at all."""
    return kiosk_payment_gate(db, kiosk) is not Gateway.CLOSED


def wallet_may_be_spent(db: Session, kiosk: Kiosk) -> bool:
    """Whether student wallet balance may be spent here.

    Top-ups land in Printvendo's account, so spending at a kiosk where the owner
    collects would mean Printvendo keeps the cash while the owner prints for
    free. Wallet is therefore only ever spendable where Printvendo is also the
    one collecting.

    The kiosk's own `accepts_wallet` flag is an additional switch on top, not an
    override: an operator may turn wallet off at a platform kiosk, but turning
    it on cannot make it legal where the owner collects.
    """
    if not kiosk.accepts_wallet:
        return False
    return kiosk_payment_gate(db, kiosk) is Gateway.PLATFORM_GATEWAY


class GateBilling:
    """The real BillingCheck the kiosks module asks for.

    Replaces `PlatformOnlyBilling`, which answered False for every owner-gateway
    kiosk so that none could go live before this existed.
    """

    def owner_can_collect(self, db: Session, kiosk: Kiosk) -> bool:
        return kiosk_payment_gate(db, kiosk) is Gateway.OWNER_GATEWAY
