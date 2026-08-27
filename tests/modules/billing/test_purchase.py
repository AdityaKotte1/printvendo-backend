"""Buying a subscription, and what activating one is allowed to do.

Until now an owner could be *granted* a trial and nothing else. When it lapsed,
`kiosk_payment_gate` answered CLOSED and their kiosk stopped selling, with no
way for them to pay to keep it open -- so this is not a billing convenience, it
is the thing that lets an owner-collecting shop exist past its trial.

Two rules carry the file. A purchase is **quoted once and frozen**, because the
plan's price may change between opening a checkout and the money arriving. And
activating **extends** whatever is already in force rather than overlapping it,
for the same reason a second trial extends the first: two live subscriptions
make "when does this stop" a question with two answers.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import BadRequest, Conflict
from app.modules.billing import (
    GRACE_DAYS,
    PURCHASE_LIFETIME,
    activate_subscription,
    active_subscription,
    grant_trial,
    has_active_subscription,
    start_purchase,
)
from app.modules.billing.models import Plan, Subscription, SubscriptionStatus
from app.modules.identity.models import User

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def owner(db_session) -> User:
    user = User(email="shopkeeper@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def plan(db_session) -> Plan:
    p = Plan(name="Pro", monthly_price=Decimal("1800.00"))
    db_session.add(p)
    db_session.flush()
    return p


# ── opening a purchase ──────────────────────────────────────────────────────


def test_a_purchase_starts_unpaid(db_session, owner, plan):
    """Nothing is in force until the money arrives. A subscription that counted
    the moment somebody opened a checkout would let a shop collect by abandoning
    one."""
    subscription = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=6, now=NOW
    )

    assert subscription.status is SubscriptionStatus.PENDING_PAYMENT
    assert has_active_subscription(db_session, owner.id) is False


def test_the_price_is_frozen_when_the_purchase_opens(db_session, owner, plan):
    """The plan's price may change while the owner is at the payment page. What
    they were quoted is what they pay, and what the invoice must be able to
    explain."""
    subscription = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=6, now=NOW
    )
    plan.monthly_price = Decimal("2500.00")
    db_session.flush()

    assert subscription.monthly_price_charged == Decimal("1800.00")
    assert subscription.total_amount == Decimal("10800.00")


def test_a_duration_nobody_sells_is_refused(db_session, owner, plan):
    with pytest.raises(BadRequest):
        start_purchase(
            db_session, user_id=owner.id, plan=plan, duration_months=7, now=NOW
        )


def test_a_second_open_purchase_is_refused(db_session, owner, plan):
    """Two checkouts for the same thing is two payments to reconcile, and the
    owner meant to buy one subscription."""
    start_purchase(db_session, user_id=owner.id, plan=plan, duration_months=6, now=NOW)

    with pytest.raises(Conflict):
        start_purchase(
            db_session, user_id=owner.id, plan=plan, duration_months=6, now=NOW
        )


# ── activating it ───────────────────────────────────────────────────────────


def test_activating_puts_the_owner_in_force(db_session, owner, plan):
    subscription = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=6, now=NOW
    )

    activate_subscription(db_session, subscription, now=NOW)

    assert subscription.status is SubscriptionStatus.ACTIVE
    assert has_active_subscription(db_session, owner.id) is True


def test_it_runs_for_the_months_that_were_paid_for(db_session, owner, plan):
    subscription = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=6, now=NOW
    )

    activate_subscription(db_session, subscription, now=NOW)

    assert subscription.starts_at == NOW
    assert subscription.expires_at == NOW + timedelta(days=180)


def test_the_grace_window_outlasts_expiry(db_session, owner, plan):
    """A few days where the shop keeps working, so a late renewal does not take
    somebody offline over a weekend."""
    subscription = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=1, now=NOW
    )

    activate_subscription(db_session, subscription, now=NOW)

    assert subscription.grace_ends_at == subscription.expires_at + timedelta(
        days=GRACE_DAYS
    )


def test_renewing_extends_rather_than_overlaps(db_session, owner, plan):
    """Paid in month one for a subscription that runs to month three: the new
    term starts when the old one ends, not today. Overlapping would sell an
    owner time they already own."""
    first = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=6, now=NOW
    )
    activate_subscription(db_session, first, now=NOW)

    second = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=6, now=NOW
    )
    activate_subscription(db_session, second, now=NOW + timedelta(days=30))

    assert second.starts_at == first.expires_at
    assert second.expires_at == first.expires_at + timedelta(days=180)


def test_renewing_after_a_lapse_starts_now(db_session, owner, plan):
    """Nothing to extend, so the new term begins today rather than being
    back-dated into time the shop spent closed."""
    lapsed = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=1, now=NOW
    )
    activate_subscription(db_session, lapsed, now=NOW)
    much_later = NOW + timedelta(days=400)

    second = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=1, now=much_later
    )
    activate_subscription(db_session, second, now=much_later)

    assert second.starts_at == much_later


def test_a_purchase_extends_a_trial_rather_than_cutting_it_short(db_session, owner, plan):
    """An owner who pays during a free trial keeps the rest of the trial. Taking
    the money and starting the clock immediately charges them for days they had
    already been given."""
    grant_trial(db_session, user_id=owner.id, plan=plan, days=30, now=NOW)
    trial_end = active_subscription(db_session, owner.id).free_until

    bought = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=1, now=NOW
    )
    activate_subscription(db_session, bought, now=NOW)

    assert bought.starts_at == trial_end


def test_activating_twice_changes_nothing(db_session, owner, plan):
    """A webhook and the browser's callback both settle the same payment, and
    both call this. The second must not extend the term a second time."""
    subscription = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=1, now=NOW
    )
    activate_subscription(db_session, subscription, now=NOW)
    expires = subscription.expires_at

    activate_subscription(db_session, subscription, now=NOW + timedelta(minutes=5))

    assert subscription.expires_at == expires


def test_a_cancelled_purchase_cannot_be_activated(db_session, owner, plan):
    subscription = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=1, now=NOW
    )
    subscription.status = SubscriptionStatus.CANCELLED
    db_session.flush()

    with pytest.raises(Conflict):
        activate_subscription(db_session, subscription, now=NOW)


def test_the_gate_opens_for_a_paid_owner(db_session, owner, plan):
    """The whole point: `kiosk_payment_gate` asks this question, and until a
    subscription could be bought the answer went false when a trial lapsed."""
    subscription = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=1, now=NOW
    )
    assert has_active_subscription(db_session, owner.id) is False

    activate_subscription(db_session, subscription, now=NOW)

    assert has_active_subscription(db_session, owner.id) is True
    assert (
        db_session.query(Subscription).filter_by(user_id=owner.id).count() == 1
    ), "a purchase is one row from checkout to expiry"


# ── a purchase nobody finished must not be a life sentence ──────────────────
#
# `PENDING_PAYMENT` was only ever set and never cleared: `activate_subscription`
# left it on success and nothing at all left it on failure. No sweep touched it,
# so one abandoned or failed checkout blocked that owner from ever buying a
# subscription again -- and `ALREADY_BUYING` told them to "wait for it to
# lapse", which nothing made happen. Found by a test payment being simulated as
# a failure and the retry being refused for ever.
#
# The age is read off `created_at`, which the database sets, so these backdate
# the row rather than winding the clock back on the caller -- passing a `now`
# from before the row was written would be testing a situation that cannot
# occur.


def _opened_at(db_session, subscription, when):
    subscription.created_at = when
    db_session.add(subscription)
    db_session.flush()
    return subscription


def test_a_purchase_nobody_finished_stops_blocking_the_next_one(db_session, plan, owner):
    """The reported bug. A checkout that failed, or that somebody simply closed,
    leaves a pending row; once it is older than the window it must not still be
    in the way."""
    stale = _opened_at(
        db_session,
        start_purchase(db_session, user_id=owner.id, plan=plan, duration_months=6),
        datetime.now(UTC) - PURCHASE_LIFETIME - timedelta(minutes=1),
    )

    fresh = start_purchase(
        db_session, user_id=owner.id, plan=plan, duration_months=6
    )

    assert fresh.id != stale.id
    assert stale.status is SubscriptionStatus.CANCELLED
    assert fresh.status is SubscriptionStatus.PENDING_PAYMENT


def test_a_purchase_opened_a_moment_ago_still_blocks(db_session, plan, owner):
    """Two live checkouts for one owner would make "what am I paying" a
    question with two answers, and a double-click is not a second purchase."""
    start_purchase(db_session, user_id=owner.id, plan=plan, duration_months=6)

    with pytest.raises(Conflict) as raised:
        start_purchase(db_session, user_id=owner.id, plan=plan, duration_months=6)

    # The sentence has to be true: it tells the owner to wait, so it says how
    # long -- and something now actually makes the waiting end.
    assert "20 minutes" in str(raised.value.detail)


def test_the_lapsed_purchase_is_cancelled_rather_than_removed(db_session, plan, owner):
    """"They tried and did not finish" is a different fact from "they never
    tried", and support needs both -- the same reason a failed payment is
    recorded rather than deleted."""
    stale = _opened_at(
        db_session,
        start_purchase(db_session, user_id=owner.id, plan=plan, duration_months=6),
        datetime.now(UTC) - PURCHASE_LIFETIME - timedelta(minutes=1),
    )
    stale_id = stale.id

    start_purchase(db_session, user_id=owner.id, plan=plan, duration_months=6)

    assert db_session.get(Subscription, stale_id) is not None


def test_a_lapsed_purchase_is_not_in_force(db_session, plan, owner):
    """Cancelling it must not be mistaken for activating it. Nothing was paid,
    so nothing may collect."""
    _opened_at(
        db_session,
        start_purchase(db_session, user_id=owner.id, plan=plan, duration_months=6),
        datetime.now(UTC) - PURCHASE_LIFETIME - timedelta(minutes=1),
    )

    start_purchase(db_session, user_id=owner.id, plan=plan, duration_months=6)

    assert has_active_subscription(db_session, owner.id) is False
