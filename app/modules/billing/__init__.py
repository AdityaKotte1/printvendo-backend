"""The billing bounded context.

Plans, what an owner pays for one, and the price band that plan imposes on their
kiosks. Import from here, never from the submodules' internals.

Billing knows nothing about kiosks. A band is answered for a *user*, and the
composition root maps that onto the `BandSource` the kiosks module asks for --
which keeps "an owner has a plan" and "a plan constrains a kiosk" as two facts
joined in one place rather than a dependency between two contexts.
"""

from app.modules.billing.bands import (
    UNBOUNDED,
    PlanPriceBand,
    price_band_for,
)
from app.modules.billing.models import (
    OwnerDiscount,
    Plan,
    PlanDiscount,
    Subscription,
    SubscriptionStatus,
)
from app.modules.billing.quotes import Quote, quote_subscription
from app.modules.billing.subscriptions import (
    active_subscription,
    effective_end,
    has_active_subscription,
    is_in_force,
    is_on_trial,
)

__all__ = [
    "UNBOUNDED",
    "OwnerDiscount",
    "Plan",
    "PlanDiscount",
    "PlanPriceBand",
    "Quote",
    "Subscription",
    "SubscriptionStatus",
    "active_subscription",
    "effective_end",
    "has_active_subscription",
    "is_in_force",
    "is_on_trial",
    "price_band_for",
    "quote_subscription",
]
