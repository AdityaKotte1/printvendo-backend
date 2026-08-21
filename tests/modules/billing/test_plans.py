"""Making the things billing has so far only been able to read.

Plans, the published discount ladder, per-owner terms, and trials all existed as
tables with readers and no writers -- `quote_subscription` priced a plan nothing
could create. These are the writes.

The one that carries money is `grant_trial`: a subscription inside its trial
counts as in force, which is half of what the payment gate requires before a
kiosk collects into its owner's own Razorpay. Granting a trial is therefore a
money-routing decision wearing a billing hat.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import BadRequest, Conflict, NotFound
from app.modules.billing import (
    SubscriptionStatus,
    has_active_subscription,
    is_on_trial,
    price_band_for,
    quote_subscription,
)
from app.modules.billing.discounts import (
    owner_discounts,
    set_owner_discount,
    set_plan_discount,
)
from app.modules.billing.plans import (
    active_plans,
    create_plan,
    plan_by_public_id,
    update_plan,
)
from app.modules.billing.subscriptions import (
    end_trial,
    grant_trial,
    set_negotiated_price,
    subscriptions_of,
)
from app.modules.identity.models import User


@pytest.fixture
def owner(db_session) -> User:
    user = User(email="owner@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def admin(db_session) -> User:
    user = User(email="admin@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def plan(db_session):
    return create_plan(
        db_session,
        name="Standard",
        monthly_price=Decimal("499.00"),
        max_kiosks=2,
        price_floor_bw=Decimal("1.00"),
        price_ceiling_bw=Decimal("5.00"),
    )


# -- plans ------------------------------------------------------------------


def test_a_plan_can_be_created_and_found_by_its_public_id(db_session, plan):
    db_session.flush()

    assert plan_by_public_id(db_session, plan.public_id).name == "Standard"


def test_a_plan_price_must_be_money_not_a_float(db_session):
    """`as_money` refuses a float rather than accepting a value that has already
    lost precision -- the whole reason money is Decimal here."""
    with pytest.raises(TypeError):
        create_plan(db_session, name="Floaty", monthly_price=499.0)


def test_a_plan_price_cannot_be_negative(db_session):
    with pytest.raises(BadRequest):
        create_plan(db_session, name="Refund Plan", monthly_price=Decimal("-1.00"))


def test_two_plans_cannot_share_a_name(db_session, plan):
    db_session.flush()

    with pytest.raises(Conflict):
        create_plan(db_session, name="Standard", monthly_price=Decimal("999.00"))


def test_a_band_that_is_upside_down_is_refused(db_session):
    """A floor above its ceiling admits no price at all, so every owner on that
    plan would be unable to set one -- and the failure would surface as a
    baffling 400 on a pricing form, a long way from here."""
    with pytest.raises(BadRequest):
        create_plan(
            db_session,
            name="Impossible",
            monthly_price=Decimal("100.00"),
            price_floor_bw=Decimal("5.00"),
            price_ceiling_bw=Decimal("1.00"),
        )


def test_a_plan_can_be_retired_without_being_deleted(db_session, plan, owner):
    """Deleting it would orphan every subscription that names it, and an invoice
    has to keep meaning something after the plan stops being sold."""
    db_session.flush()
    update_plan(db_session, plan, is_active=False)
    db_session.flush()

    assert active_plans(db_session) == []
    assert plan_by_public_id(db_session, plan.public_id) is not None
    with pytest.raises(BadRequest):
        quote_subscription(
            db_session, user_id=owner.id, plan=plan, duration_months=12
        )


def test_an_unknown_plan_is_not_found(db_session):
    with pytest.raises(NotFound):
        plan_by_public_id(db_session, "sub_0000000000000000")


def test_a_plans_band_reaches_the_owner_on_it(db_session, plan, owner):
    """The band is a commercial term of the plan, and the only path from one to
    an owner's kiosk prices."""
    db_session.flush()
    grant_trial(db_session, user_id=owner.id, plan=plan, days=30)
    db_session.flush()

    band = price_band_for(db_session, owner.id)

    assert band.floor_bw == Decimal("1.00")
    assert band.ceiling_bw == Decimal("5.00")


# -- discounts --------------------------------------------------------------


def test_the_published_ladder_applies_to_everyone_on_the_plan(db_session, plan, owner):
    db_session.flush()
    set_plan_discount(db_session, plan, duration_months=12, percent=Decimal("10.00"))
    db_session.flush()

    quote = quote_subscription(
        db_session, user_id=owner.id, plan=plan, duration_months=12
    )

    assert quote.discount_source == "plan"
    assert quote.total == Decimal("5389.20")


def test_an_owners_own_rate_beats_the_published_one(db_session, plan, owner, admin):
    """D13, and the thing the old system could not express: one owner on a
    different annual rate, which is the ordinary shape of a real negotiation."""
    db_session.flush()
    set_plan_discount(db_session, plan, duration_months=12, percent=Decimal("10.00"))
    set_owner_discount(
        db_session,
        user_id=owner.id,
        duration_months=12,
        percent=Decimal("25.00"),
        granted_by=admin.id,
        note="signed three shops",
    )
    db_session.flush()

    quote = quote_subscription(
        db_session, user_id=owner.id, plan=plan, duration_months=12
    )

    assert quote.discount_source == "owner"
    assert quote.total == Decimal("4491.00")


def test_setting_the_same_owner_rate_twice_replaces_it(db_session, owner, admin):
    """One rate per owner per duration. Two rows would make "what does this
    owner pay" a question with two answers."""
    set_owner_discount(
        db_session,
        user_id=owner.id,
        duration_months=12,
        percent=Decimal("25.00"),
        granted_by=admin.id,
    )
    db_session.flush()
    set_owner_discount(
        db_session,
        user_id=owner.id,
        duration_months=12,
        percent=Decimal("30.00"),
        granted_by=admin.id,
    )
    db_session.flush()

    rates = owner_discounts(db_session, owner.id)

    assert len(rates) == 1
    assert rates[0].percent == Decimal("30.00")


def test_a_discount_outside_nought_to_a_hundred_is_refused(db_session, owner, admin):
    """101 percent is money flowing the wrong way."""
    for percent in (Decimal("-1.00"), Decimal("101.00")):
        with pytest.raises(BadRequest):
            set_owner_discount(
                db_session,
                user_id=owner.id,
                duration_months=12,
                percent=percent,
                granted_by=admin.id,
            )


def test_a_discount_for_a_duration_nobody_can_buy_is_refused(db_session, owner, admin):
    """Subscriptions are sold for 1, 6 or 12 months. A rate for 9 would never be
    applied to anything and would sit in the console looking like a term
    somebody had been granted."""
    with pytest.raises(BadRequest):
        set_owner_discount(
            db_session,
            user_id=owner.id,
            duration_months=9,
            percent=Decimal("10.00"),
            granted_by=admin.id,
        )


# -- trials -----------------------------------------------------------------


def test_a_trial_makes_an_owner_able_to_collect(db_session, plan, owner, admin):
    """The point of the whole feature: a trialling owner's SOLD kiosk can pass
    the payment gate and take money into their own account."""
    db_session.flush()

    subscription = grant_trial(
        db_session, user_id=owner.id, plan=plan, days=30
    )
    db_session.flush()

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert is_on_trial(subscription) is True
    assert has_active_subscription(db_session, owner.id) is True


def test_a_trial_costs_nothing_and_says_so(db_session, plan, owner, admin):
    """Charged zero rather than the plan price with a flag beside it: an invoice
    that says 499 and was never collected is how a billing dispute starts."""
    db_session.flush()

    subscription = grant_trial(
        db_session, user_id=owner.id, plan=plan, days=30
    )

    assert subscription.total_amount == Decimal("0.00")
    assert subscription.monthly_price_charged == Decimal("0.00")


def test_a_trial_of_no_days_is_refused(db_session, plan, owner, admin):
    db_session.flush()

    for days in (0, -5):
        with pytest.raises(BadRequest):
            grant_trial(
                db_session, user_id=owner.id, plan=plan, days=days
            )


def test_an_expired_trial_stops_entitling_anybody(db_session, plan, owner, admin):
    db_session.flush()
    subscription = grant_trial(
        db_session, user_id=owner.id, plan=plan, days=30
    )
    subscription.free_until = datetime.now(UTC) - timedelta(days=1)
    db_session.flush()

    assert has_active_subscription(db_session, owner.id) is False


def test_a_trial_can_be_ended_early(db_session, plan, owner, admin):
    """A shop that turns out to be selling something else entirely should stop
    collecting today, not in three weeks."""
    db_session.flush()
    subscription = grant_trial(
        db_session, user_id=owner.id, plan=plan, days=30
    )
    db_session.flush()

    end_trial(db_session, subscription)
    db_session.flush()

    assert has_active_subscription(db_session, owner.id) is False


def test_extending_a_trial_moves_the_end_rather_than_adding_a_second(
    db_session, plan, owner, admin
):
    """Two live trials for one owner would make "when does this stop being
    free" a question with two answers, and `active_subscription` would take the
    longer -- so the shorter one could never end anything."""
    db_session.flush()
    first = grant_trial(
        db_session, user_id=owner.id, plan=plan, days=30
    )
    db_session.flush()

    second = grant_trial(
        db_session, user_id=owner.id, plan=plan, days=60
    )
    db_session.flush()

    assert second.id == first.id
    assert len(subscriptions_of(db_session, owner.id)) == 1
    assert second.free_until > first.starts_at + timedelta(days=59)


# -- a negotiated price -----------------------------------------------------


def test_a_negotiated_price_is_what_gets_charged(db_session, plan, owner, admin):
    db_session.flush()
    subscription = grant_trial(
        db_session, user_id=owner.id, plan=plan, days=30
    )
    db_session.flush()

    set_negotiated_price(db_session, subscription, Decimal("299.00"))
    db_session.flush()

    quote = quote_subscription(
        db_session,
        user_id=owner.id,
        plan=plan,
        duration_months=12,
        negotiated_price=subscription.negotiated_price,
    )

    assert quote.negotiated is True
    assert quote.total == Decimal("3588.00")


def test_a_negotiated_price_cannot_be_negative(db_session, plan, owner, admin):
    db_session.flush()
    subscription = grant_trial(
        db_session, user_id=owner.id, plan=plan, days=30
    )

    with pytest.raises(BadRequest):
        set_negotiated_price(db_session, subscription, Decimal("-10.00"))
