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
from app.modules.billing.discounts import (
    clear_owner_discount,
    owner_discounts,
    plan_discounts,
    set_owner_discount,
    set_plan_discount,
)
from app.modules.billing.models import (
    OwnerDiscount,
    Plan,
    PlanDiscount,
    Subscription,
    SubscriptionStatus,
)
from app.modules.billing.plans import (
    active_plans,
    all_plans,
    create_plan,
    plan_by_public_id,
    update_plan,
)
from app.modules.billing.purchase import (
    DAYS_PER_MONTH,
    GRACE_DAYS,
    activate_subscription,
    start_purchase,
)
from app.modules.billing.quotes import ALLOWED_DURATIONS, Quote, quote_subscription
from app.modules.billing.subscriptions import (
    active_subscription,
    effective_end,
    end_trial,
    grant_trial,
    has_active_subscription,
    is_in_force,
    is_on_trial,
    set_negotiated_price,
    subscriptions_of,
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
    "ALLOWED_DURATIONS",
    "DAYS_PER_MONTH",
    "GRACE_DAYS",
    "activate_subscription",
    "active_plans",
    "all_plans",
    "clear_owner_discount",
    "create_plan",
    "end_trial",
    "grant_trial",
    "owner_discounts",
    "plan_by_public_id",
    "plan_discounts",
    "set_negotiated_price",
    "set_owner_discount",
    "set_plan_discount",
    "start_purchase",
    "subscriptions_of",
    "update_plan",
    "quote_subscription",
]
