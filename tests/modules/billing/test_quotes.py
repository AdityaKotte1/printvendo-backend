from decimal import Decimal

import pytest

from app.core.errors import BadRequest
from app.modules.billing.models import OwnerDiscount, Plan, PlanDiscount
from app.modules.billing.quotes import quote_subscription
from app.modules.identity.models import User


@pytest.fixture
def owner(db_session) -> User:
    user = User(email="owner@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def plan(db_session) -> Plan:
    p = Plan(name="Pro", monthly_price=Decimal("1800.00"))
    db_session.add(p)
    db_session.flush()
    return p


def _plan_discount(db_session, plan, months, percent):
    db_session.add(
        PlanDiscount(plan_id=plan.id, duration_months=months, percent=Decimal(percent))
    )
    db_session.flush()


def _owner_discount(db_session, owner, months, percent):
    db_session.add(
        OwnerDiscount(
            user_id=owner.id, duration_months=months, percent=Decimal(percent)
        )
    )
    db_session.flush()


def test_a_single_month_at_list_price(db_session, owner, plan):
    q = quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=1)
    assert q.total == Decimal("1800.00")
    assert q.discount_source == "none"


def test_twelve_months_with_no_discount_is_just_twelve_times(db_session, owner, plan):
    q = quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=12)
    assert q.total == Decimal("21600.00")


def test_a_plan_discount_applies(db_session, owner, plan):
    _plan_discount(db_session, plan, 12, 10)
    q = quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=12)

    assert q.discount_percent == Decimal("10.00")
    assert q.discount_source == "plan"
    assert q.total == Decimal("19440.00")  # 21600 less 10%


def test_an_owner_discount_beats_the_plan_discount(db_session, owner, plan):
    """The whole point of D13: one owner can be given a different annual rate
    without moving everyone else on the plan."""
    _plan_discount(db_session, plan, 12, 10)
    _owner_discount(db_session, owner, 12, 25)

    q = quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=12)
    assert q.discount_source == "owner"
    assert q.total == Decimal("16200.00")  # 21600 less 25%


def test_an_owner_discount_for_one_duration_does_not_leak_to_another(
    db_session, owner, plan
):
    _owner_discount(db_session, owner, 12, 25)
    q = quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=6)
    assert q.discount_source == "none"


def test_one_owners_discount_does_not_apply_to_another(db_session, owner, plan):
    other = User(email="other@example.com", hashed_password="x")
    db_session.add(other)
    db_session.flush()
    _owner_discount(db_session, owner, 12, 50)

    q = quote_subscription(db_session, user_id=other.id, plan=plan, duration_months=12)
    assert q.discount_source == "none"
    assert q.total == Decimal("21600.00")


def test_a_negotiated_price_replaces_the_plan_rate(db_session, owner, plan):
    q = quote_subscription(
        db_session,
        user_id=owner.id,
        plan=plan,
        duration_months=12,
        negotiated_price=Decimal("1000.00"),
    )
    assert q.monthly_price == Decimal("1000.00")
    assert q.total == Decimal("12000.00")
    assert q.negotiated is True


def test_a_negotiated_price_and_an_owner_discount_stack(db_session, owner, plan):
    """A bespoke rate and a bespoke annual discount are separate levers, and a
    real negotiation often uses both."""
    _owner_discount(db_session, owner, 12, 20)

    q = quote_subscription(
        db_session,
        user_id=owner.id,
        plan=plan,
        duration_months=12,
        negotiated_price=Decimal("1000.00"),
    )
    assert q.total == Decimal("9600.00")  # 12000 less 20%


def test_a_free_plan_is_priced_at_zero(db_session, owner):
    free = Plan(name="Pilot", monthly_price=Decimal("0.00"))
    db_session.add(free)
    db_session.flush()

    q = quote_subscription(db_session, user_id=owner.id, plan=free, duration_months=6)
    assert q.total == Decimal("0.00")


def test_a_full_discount_is_allowed(db_session, owner, plan):
    _owner_discount(db_session, owner, 12, 100)
    q = quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=12)
    assert q.total == Decimal("0.00")


def test_an_unsupported_duration_is_refused(db_session, owner, plan):
    with pytest.raises(BadRequest):
        quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=3)


def test_a_retired_plan_cannot_be_quoted(db_session, owner, plan):
    plan.is_active = False
    db_session.flush()
    with pytest.raises(BadRequest):
        quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=1)


def test_a_negative_negotiated_price_is_refused(db_session, owner, plan):
    with pytest.raises(BadRequest):
        quote_subscription(
            db_session,
            user_id=owner.id,
            plan=plan,
            duration_months=1,
            negotiated_price=Decimal("-5"),
        )


def test_a_discount_over_one_hundred_percent_is_refused(db_session, owner, plan):
    """Otherwise the total goes negative and the platform owes the owner money."""
    _owner_discount(db_session, owner, 12, 150)
    with pytest.raises(BadRequest):
        quote_subscription(db_session, user_id=owner.id, plan=plan, duration_months=12)


def test_totals_are_quantised_to_paise(db_session, owner, plan):
    odd = Plan(name="Odd", monthly_price=Decimal("333.33"))
    db_session.add(odd)
    db_session.flush()
    _plan_discount(db_session, odd, 6, 7)

    q = quote_subscription(db_session, user_id=owner.id, plan=odd, duration_months=6)
    assert q.total == q.total.quantize(Decimal("0.01"))


def test_the_quote_explains_itself(db_session, owner, plan):
    """An invoice that cannot say which rate and which discount applied is how
    billing arguments start."""
    _owner_discount(db_session, owner, 6, 15)
    q = quote_subscription(
        db_session,
        user_id=owner.id,
        plan=plan,
        duration_months=6,
        negotiated_price=Decimal("1500"),
    )

    assert q.duration_months == 6
    assert q.monthly_price == Decimal("1500.00")
    assert q.discount_percent == Decimal("15.00")
    assert q.discount_source == "owner"
    assert q.negotiated is True
