"""Creating and retiring the tiers an owner can be on.

`quote_subscription` has been pricing plans since billing landed, and until now
nothing could make one. These are the writes.

A plan is **retired, never deleted**. Subscriptions name a plan, and an invoice
has to keep meaning something after the plan stops being sold -- deleting the row
would leave a charge that cannot say what it was for. `is_active` is the switch,
and `quote_subscription` already refuses an inactive plan, so retiring one closes
the sale without disturbing anybody already on it.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict, NotFound
from app.core.money import as_money
from app.modules.billing.models import Plan

NO_SUCH_PLAN = "That plan does not exist."


def _check_band(floor: Decimal | None, ceiling: Decimal | None, colour: str) -> None:
    """Refuse a band that admits no price at all.

    A floor above its ceiling makes every price an owner could type invalid, and
    the failure surfaces as a baffling 400 on a pricing form a long way from the
    plan that caused it.
    """
    if floor is not None and floor < 0:
        raise BadRequest(f"The {colour} price floor cannot be negative.")
    if floor is not None and ceiling is not None and floor > ceiling:
        raise BadRequest(
            f"The {colour} price floor cannot be above the {colour} ceiling."
        )


def _as_band_money(value: Decimal | None) -> Decimal | None:
    return None if value is None else as_money(value)


def create_plan(
    db: Session,
    *,
    name: str,
    monthly_price: Decimal,
    max_kiosks: int = 1,
    price_floor_bw: Decimal | None = None,
    price_ceiling_bw: Decimal | None = None,
    price_floor_color: Decimal | None = None,
    price_ceiling_color: Decimal | None = None,
) -> Plan:
    """A new tier, active from the moment it exists."""
    name = name.strip()
    if not name:
        raise BadRequest("A plan needs a name.")

    price = as_money(monthly_price)
    if price < 0:
        raise BadRequest("A plan price cannot be negative.")

    if max_kiosks < 1:
        raise BadRequest("A plan has to allow at least one kiosk.")

    _check_band(price_floor_bw, price_ceiling_bw, "black and white")
    _check_band(price_floor_color, price_ceiling_color, "colour")

    if db.execute(select(Plan).where(Plan.name == name)).scalar_one_or_none():
        raise Conflict(f"A plan called {name!r} already exists.")

    plan = Plan(
        name=name,
        monthly_price=price,
        max_kiosks=max_kiosks,
        price_floor_bw=_as_band_money(price_floor_bw),
        price_ceiling_bw=_as_band_money(price_ceiling_bw),
        price_floor_color=_as_band_money(price_floor_color),
        price_ceiling_color=_as_band_money(price_ceiling_color),
    )
    db.add(plan)
    db.flush()
    return plan


# What `update_plan` will not change, and why: `name` is what an owner sees on
# an invoice they already have. Renaming a plan silently rewrites history.
def update_plan(
    db: Session,
    plan: Plan,
    *,
    monthly_price: Decimal | None = None,
    max_kiosks: int | None = None,
    price_floor_bw: Decimal | None = None,
    price_ceiling_bw: Decimal | None = None,
    price_floor_color: Decimal | None = None,
    price_ceiling_color: Decimal | None = None,
    is_active: bool | None = None,
) -> Plan:
    """Change a plan's terms, or take it off sale.

    Changing the price does **not** change what anybody is already paying:
    `Subscription.monthly_price_charged` is stored at purchase precisely so a
    later price change cannot rewrite an existing charge.
    """
    if monthly_price is not None:
        price = as_money(monthly_price)
        if price < 0:
            raise BadRequest("A plan price cannot be negative.")
        plan.monthly_price = price

    if max_kiosks is not None:
        if max_kiosks < 1:
            raise BadRequest("A plan has to allow at least one kiosk.")
        plan.max_kiosks = max_kiosks

    floor_bw = price_floor_bw if price_floor_bw is not None else plan.price_floor_bw
    ceiling_bw = (
        price_ceiling_bw if price_ceiling_bw is not None else plan.price_ceiling_bw
    )
    floor_color = (
        price_floor_color if price_floor_color is not None else plan.price_floor_color
    )
    ceiling_color = (
        price_ceiling_color
        if price_ceiling_color is not None
        else plan.price_ceiling_color
    )
    _check_band(floor_bw, ceiling_bw, "black and white")
    _check_band(floor_color, ceiling_color, "colour")

    plan.price_floor_bw = _as_band_money(floor_bw)
    plan.price_ceiling_bw = _as_band_money(ceiling_bw)
    plan.price_floor_color = _as_band_money(floor_color)
    plan.price_ceiling_color = _as_band_money(ceiling_color)

    if is_active is not None:
        plan.is_active = is_active

    db.add(plan)
    db.flush()
    return plan


def active_plans(db: Session) -> list[Plan]:
    """What can be sold today, cheapest first."""
    return list(
        db.execute(
            select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.monthly_price)
        ).scalars()
    )


def all_plans(db: Session) -> list[Plan]:
    """Everything, retired ones included -- an admin needs to see what somebody
    is still on."""
    return list(db.execute(select(Plan).order_by(Plan.monthly_price)).scalars())


def plan_named(db: Session, plan_id: int) -> str:
    """What to call this plan on a document.

    A name rather than the row, and never a raise: a plan retired years after
    somebody bought it must not turn their invoice into a 500. `Subscription`
    has no relationship to `Plan` on purpose -- the api layer may not touch
    either table -- so this is how a document gets the word.
    """
    plan = db.get(Plan, plan_id)
    return plan.name if plan is not None else "Subscription"


def plan_by_public_id(db: Session, public_id: str) -> Plan:
    """The plan with this id, retired or not.

    Retired plans resolve on purpose: a subscription that names one still has to
    be readable, and refusing here would make an owner's own billing page 404
    because of a commercial decision taken after they bought.
    """
    plan = db.execute(
        select(Plan).where(Plan.public_id == public_id)
    ).scalar_one_or_none()
    if plan is None:
        raise NotFound(NO_SUCH_PLAN)
    return plan
