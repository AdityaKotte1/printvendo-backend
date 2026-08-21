"""Whether an owner's subscription is currently in force.

One predicate. The old backend asked this question in
`services/gateway_routing.py`, again in `routers/subscription.py`, and inline in
places that needed it -- and the copies disagreed, which is how the wallet money
leak happened.

"In force" covers three cases that all mean the owner is entitled to collect:

  * a paid subscription inside its term
  * a paid subscription inside its grace window, so a late renewal does not take
    a shop offline over a weekend
  * a trial (`free_until`), which is unbilled but entitles the owner exactly the
    same way -- see D13
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.core.money import as_money
from app.modules.billing.models import Plan, Subscription, SubscriptionStatus


def effective_end(subscription: Subscription) -> datetime | None:
    """The moment this subscription stops entitling the owner to anything.

    Grace, where present, extends past expiry. A trial can extend past both --
    an owner given three free months keeps them even if the paid term they were
    sold has technically elapsed.
    """
    candidates = [
        d
        for d in (
            subscription.free_until,
            subscription.grace_ends_at,
            subscription.expires_at,
        )
        if d is not None
    ]
    return max(candidates) if candidates else None


def is_in_force(subscription: Subscription, *, now: datetime | None = None) -> bool:
    """Whether this subscription entitles its owner to collect payments."""
    now = now or datetime.now(UTC)

    if subscription.status is not SubscriptionStatus.ACTIVE:
        return False

    end = effective_end(subscription)
    if end is None:
        # ACTIVE with no end date at all is malformed data, not a perpetual
        # licence. Treated as not in force, deliberately: the failure modes are
        # not symmetric. Refusing wrongly takes a shop offline, which is loud
        # and fixed in minutes. Allowing wrongly lets an owner collect without
        # paying, indefinitely and silently.
        return False

    return now < end


def active_subscription(db: Session, user_id: int) -> Subscription | None:
    """This owner's in-force subscription, if they have one.

    Returns the one that runs longest, so an owner who renewed early -- leaving
    an old and a new row both ACTIVE -- is judged on the better of the two
    rather than on whichever the database happened to return first.
    """
    stmt = select(Subscription).where(
        Subscription.user_id == user_id,
        Subscription.status == SubscriptionStatus.ACTIVE,
    )
    candidates = [s for s in db.execute(stmt).scalars() if is_in_force(s)]
    if not candidates:
        return None

    return max(candidates, key=lambda s: effective_end(s) or datetime.min.replace(tzinfo=UTC))


def has_active_subscription(db: Session, user_id: int) -> bool:
    return active_subscription(db, user_id) is not None


def is_on_trial(subscription: Subscription, *, now: datetime | None = None) -> bool:
    """Whether this subscription is currently unbilled.

    Worth being able to ask separately: a trialling owner collects real money
    into their own account while paying nothing, so admin views should be able
    to show it rather than having it hidden inside "active".
    """
    now = now or datetime.now(UTC)
    return subscription.free_until is not None and now < subscription.free_until


def subscriptions_of(db: Session, user_id: int) -> list[Subscription]:
    """Everything this owner has ever been on, newest first."""
    return list(
        db.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.id.desc())
        ).scalars()
    )


def grant_trial(
    db: Session,
    *,
    user_id: int,
    plan: Plan,
    days: int,
    now: datetime | None = None,
) -> Subscription:
    """Let an owner collect for `days` without paying (D13).

    A money-routing lever wearing a billing hat: a subscription inside its trial
    is in force, which is half of what `kiosk_payment_gate` requires before a
    SOLD or SAAS kiosk collects into its owner's own Razorpay. Granting one lets
    that shop go live; it lapsing suspends the kiosk again.

    Who granted it is recorded by `ops.audit` at the route, not in a column
    here. Every subscription restored from the legacy dump would have a null in
    such a column, and a field that is empty for most rows is a field nobody
    trusts -- the audit trail answers "who did this" for every mutation under
    one rule rather than this table answering it for one.

    Charged **zero**, not the plan price with a flag beside it. A row saying 499
    against money nobody collected is how a billing dispute starts, and
    `monthly_price_charged` is what an invoice reads.

    Granting a second trial **extends the first** rather than adding a row. Two
    live trials would make "when does this stop being free" a question with two
    answers, and `active_subscription` takes the longest-running -- so the
    shorter one could never end anything, including by being cancelled.
    """
    if days < 1:
        raise BadRequest("A trial has to last at least a day.")

    now = now or datetime.now(UTC)
    ends = now + timedelta(days=days)

    existing = active_subscription(db, user_id)
    if existing is not None and existing.free_until is not None:
        # Extend, never shorten: an owner told they had until March should not
        # lose a month because somebody granted "30 days" again in February.
        existing.free_until = max(existing.free_until, ends)
        existing.plan_id = plan.id
        db.add(existing)
        db.flush()
        return existing

    subscription = Subscription(
        user_id=user_id,
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        duration_months=1,
        monthly_price_charged=Decimal("0.00"),
        discount_percent=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        free_until=ends,
        starts_at=now,
    )
    db.add(subscription)
    db.flush()
    return subscription


def end_trial(
    db: Session, subscription: Subscription, *, now: datetime | None = None
) -> Subscription:
    """Stop a trial today rather than in three weeks.

    Sets `free_until` to now rather than clearing it, so the row still records
    that there *was* a trial and when it stopped. Clearing it would leave an
    ACTIVE subscription with no end date at all, which `is_in_force` reads as
    malformed and refuses -- the right answer by luck rather than by design.
    """
    subscription.free_until = now or datetime.now(UTC)
    db.add(subscription)
    db.flush()
    return subscription


def set_negotiated_price(
    db: Session, subscription: Subscription, price: Decimal | None
) -> Subscription:
    """What this owner pays a month, whatever the plan says (D13).

    None puts them back on the plan's price. The value is applied by
    `quote_subscription`, which is the only thing that prices anything -- this
    records the term, it does not do the arithmetic.
    """
    if price is not None:
        price = as_money(price)
        if price < 0:
            raise BadRequest("A subscription price cannot be negative.")

    subscription.negotiated_price = price
    db.add(subscription)
    db.flush()
    return subscription
