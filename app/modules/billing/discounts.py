"""Percent off, published and negotiated.

Two tables, one subject. `PlanDiscount` is the list price -- everyone on the plan
who pays for twelve months gets it. `OwnerDiscount` is what one owner was
actually promised, and it wins.

Writing both here rather than beside their readers is what keeps the two rules
that govern them in one place: a percent is between 0 and 100, and it is for a
duration somebody can actually buy. A rate for nine months would never be
applied to anything, and would sit in an admin console looking like a term an
owner had been granted.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.core.money import as_money
from app.modules.billing.models import OwnerDiscount, Plan, PlanDiscount
from app.modules.billing.quotes import ALLOWED_DURATIONS


def _check(duration_months: int, percent: Decimal) -> Decimal:
    if duration_months not in ALLOWED_DURATIONS:
        allowed = ", ".join(str(d) for d in ALLOWED_DURATIONS)
        raise BadRequest(f"Subscriptions are sold for {allowed} months.")

    percent = as_money(percent)
    if not (0 <= percent <= 100):
        raise BadRequest("A discount must be between 0 and 100 percent.")
    return percent


def set_plan_discount(
    db: Session, plan: Plan, *, duration_months: int, percent: Decimal
) -> PlanDiscount:
    """The published rate for paying this many months up front.

    Replaces any existing rate for that duration rather than adding a second:
    `uq_plan_duration` would refuse the insert anyway, and two rows would make
    "what does this plan cost for a year" a question with two answers.
    """
    percent = _check(duration_months, percent)

    existing = db.execute(
        select(PlanDiscount).where(
            PlanDiscount.plan_id == plan.id,
            PlanDiscount.duration_months == duration_months,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.percent = percent
        db.add(existing)
        db.flush()
        return existing

    discount = PlanDiscount(
        plan_id=plan.id, duration_months=duration_months, percent=percent
    )
    db.add(discount)
    db.flush()
    return discount


def set_owner_discount(
    db: Session,
    *,
    user_id: int,
    duration_months: int,
    percent: Decimal,
    granted_by: int | None,
    note: str | None = None,
) -> OwnerDiscount:
    """What one owner was promised, overriding the published ladder (D13).

    The old system could change an owner's monthly price, or change a discount
    for everyone on a plan, but not give one owner a different annual rate --
    which is the ordinary shape of a real commercial negotiation, so it happened
    anyway, in side agreements nothing enforced.

    `granted_by` and `note` are why it exists: a rate nobody can explain in a
    year's time is a rate somebody will argue about.
    """
    percent = _check(duration_months, percent)

    existing = db.execute(
        select(OwnerDiscount).where(
            OwnerDiscount.user_id == user_id,
            OwnerDiscount.duration_months == duration_months,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.percent = percent
        existing.note = note
        existing.granted_by_user_id = granted_by
        db.add(existing)
        db.flush()
        return existing

    discount = OwnerDiscount(
        user_id=user_id,
        duration_months=duration_months,
        percent=percent,
        note=note,
        granted_by_user_id=granted_by,
    )
    db.add(discount)
    db.flush()
    return discount


def clear_owner_discount(db: Session, *, user_id: int, duration_months: int) -> None:
    """Put this owner back on the published ladder."""
    db.query(OwnerDiscount).filter(
        OwnerDiscount.user_id == user_id,
        OwnerDiscount.duration_months == duration_months,
    ).delete()


def owner_discounts(db: Session, user_id: int) -> list[OwnerDiscount]:
    """Every rate negotiated with this owner, shortest term first."""
    return list(
        db.execute(
            select(OwnerDiscount)
            .where(OwnerDiscount.user_id == user_id)
            .order_by(OwnerDiscount.duration_months)
        ).scalars()
    )


def plan_discounts(db: Session, plan: Plan) -> list[PlanDiscount]:
    """The published ladder for one plan, shortest term first."""
    return list(
        db.execute(
            select(PlanDiscount)
            .where(PlanDiscount.plan_id == plan.id)
            .order_by(PlanDiscount.duration_months)
        ).scalars()
    )
