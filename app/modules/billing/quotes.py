"""What a subscription costs this owner, for this many months.

There is exactly one function that answers that question. The old backend
computed subscription totals in `routers/subscription.py` with a fallback to
hardcoded constants when the plan table was empty, which meant the price a
customer saw and the price they were charged could be produced by two different
code paths.

Resolution order, most specific first:

  monthly price   negotiated_price on the subscription  ->  plan.monthly_price
  discount        OwnerDiscount(user, months)           ->  PlanDiscount(plan, months)  ->  none

`negotiated_price` replaces the plan rate *before* any discount applies, so an
owner can hold both a bespoke rate and a bespoke annual discount -- which is the
ordinary shape of a real negotiation.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.core.money import as_money
from app.modules.billing.models import OwnerDiscount, Plan, PlanDiscount

# Months an owner may buy in one go. Anything else is a typo or an attempt to
# find a rounding edge.
ALLOWED_DURATIONS = (1, 6, 12)


@dataclass(frozen=True)
class Quote:
    """A price, and every input that produced it.

    The breakdown is returned rather than just the total because an owner
    disputing a charge deserves to see which rate and which discount applied,
    and because an invoice that cannot explain itself is how billing arguments
    start.
    """

    duration_months: int
    monthly_price: Decimal
    discount_percent: Decimal
    total: Decimal

    negotiated: bool
    discount_source: str  # "owner", "plan", or "none"


def _discount_for(
    db: Session, *, user_id: int, plan_id: int, duration_months: int
) -> tuple[Decimal, str]:
    owner = db.execute(
        select(OwnerDiscount.percent).where(
            OwnerDiscount.user_id == user_id,
            OwnerDiscount.duration_months == duration_months,
        )
    ).scalar_one_or_none()
    if owner is not None:
        return Decimal(owner), "owner"

    plan = db.execute(
        select(PlanDiscount.percent).where(
            PlanDiscount.plan_id == plan_id,
            PlanDiscount.duration_months == duration_months,
        )
    ).scalar_one_or_none()
    if plan is not None:
        return Decimal(plan), "plan"

    return Decimal("0"), "none"


def quote_subscription(
    db: Session,
    *,
    user_id: int,
    plan: Plan,
    duration_months: int,
    negotiated_price: Decimal | None = None,
) -> Quote:
    """Price a subscription. The single source of truth for what is charged."""
    if duration_months not in ALLOWED_DURATIONS:
        allowed = ", ".join(str(d) for d in ALLOWED_DURATIONS)
        raise BadRequest(f"Subscriptions are sold for {allowed} months.")

    if not plan.is_active:
        raise BadRequest(f"The {plan.name} plan is no longer available.")

    monthly = as_money(
        negotiated_price if negotiated_price is not None else plan.monthly_price
    )
    if monthly < 0:
        raise BadRequest("A subscription price cannot be negative.")

    percent, source = _discount_for(
        db, user_id=user_id, plan_id=plan.id, duration_months=duration_months
    )
    if not (0 <= percent <= 100):
        raise BadRequest("A discount must be between 0 and 100 percent.")

    gross = monthly * duration_months
    total = as_money(gross - (gross * percent / Decimal("100")))

    return Quote(
        duration_months=duration_months,
        monthly_price=monthly,
        discount_percent=as_money(percent),
        total=total,
        negotiated=negotiated_price is not None,
        discount_source=source,
    )
