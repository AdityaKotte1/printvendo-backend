"""What an owner's plan allows them to charge.

Price floors and ceilings are a commercial term of the subscription, not a
property of the machine -- the same printer on a different plan may charge
differently. So the band is read from the plan the owner is currently paying
for, never from the kiosk.

Returned as billing's own small type rather than as a `Plan`, and taking a
`user_id` rather than a `Kiosk`, so this module still knows nothing about
kiosks. The composition root maps this onto the `BandSource` the kiosks module
asks for -- which is the only place that has any business knowing both that an
owner has a plan and that a plan constrains a kiosk.

**No subscription means unbounded, not zero.** A PLATFORM kiosk has no
subscribing owner at all and its prices are set by the platform; failing closed
here would make every platform kiosk unpriceable. A SOLD or SAAS kiosk cannot be
LIVE without an active subscription -- the payment gate already refuses -- so
the unbounded case is unreachable for exactly the kiosks a band is meant to
constrain.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.billing.models import Plan
from app.modules.billing.subscriptions import active_subscription


@dataclass(frozen=True)
class PlanPriceBand:
    """The four bounds a plan imposes. None means unbounded on that side."""

    floor_bw: Decimal | None
    ceiling_bw: Decimal | None
    floor_color: Decimal | None
    ceiling_color: Decimal | None


UNBOUNDED = PlanPriceBand(None, None, None, None)


def price_band_for(db: Session, user_id: int) -> PlanPriceBand:
    """The band this owner's current plan imposes.

    Unbounded when they have no plan in force. See the module docstring for why
    that is the correct direction to fail.
    """
    subscription = active_subscription(db, user_id)
    if subscription is None:
        return UNBOUNDED

    plan = db.execute(
        select(Plan).where(Plan.id == subscription.plan_id)
    ).scalar_one_or_none()
    if plan is None:
        return UNBOUNDED

    return PlanPriceBand(
        floor_bw=plan.price_floor_bw,
        ceiling_bw=plan.price_ceiling_bw,
        floor_color=plan.price_floor_color,
        ceiling_color=plan.price_ceiling_color,
    )
