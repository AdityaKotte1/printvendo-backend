"""Buying a subscription, and putting it in force when the money arrives.

Until this existed an owner could only be *granted* a trial. When the trial
lapsed `kiosk_payment_gate` answered CLOSED and their kiosk stopped selling,
with no way for them to pay to keep it open -- so this is not a billing
convenience. It is what lets an owner-collecting shop exist past its trial.

Two rules, both about not selling the same time twice.

**A purchase is quoted once and frozen.** `monthly_price_charged`,
`discount_percent` and `total_amount` are written when the checkout opens, not
read from the plan when the payment settles: a plan's price may change while an
owner is at the payment page, and an invoice must say what was actually paid.

**Activating extends whatever is already in force.** Not from today -- from the
end of the current term, trial included. Starting a bought term immediately
would charge an owner for days they already held, and leaving two ACTIVE rows
overlapping would make "when does this stop" a question with two answers.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.modules.billing.models import Plan, Subscription, SubscriptionStatus
from app.modules.billing.quotes import quote_subscription
from app.modules.billing.subscriptions import active_subscription

# A month, for billing. Calendar months differ in length and an owner buying
# "three months" is buying ninety days of being able to collect -- a term that
# depends on which month it was bought in is a term nobody can check.
DAYS_PER_MONTH = 30

# Days past expiry where the kiosk keeps working, so a late renewal does not
# take a shop offline over a weekend.
GRACE_DAYS = 5

# How long an unpaid purchase holds the slot, mirroring `ORDER_LIFETIME` and
# for the same reason: it is somebody standing at a payment page, not a
# commitment. Long enough for a slow card and a re-entered OTP; short enough
# that a shop that gave up is not locked out of the thing that keeps it selling.
PURCHASE_LIFETIME = timedelta(minutes=20)

ALREADY_BUYING = (
    "There is already a payment open for this subscription. Finish it, or wait "
    f"{int(PURCHASE_LIFETIME.total_seconds() // 60)} minutes for it to lapse, "
    "before starting another."
)
NOT_PURCHASABLE = "That subscription is not waiting to be paid for."


def start_purchase(
    db: Session,
    *,
    user_id: int,
    plan: Plan,
    duration_months: int,
    now: datetime | None = None,
) -> Subscription:
    """Open an unpaid subscription at today's quoted price.

    Nothing is in force yet: a subscription that counted from the moment a
    checkout opened would let a shop collect real money by abandoning one.
    """
    now = now or datetime.now(UTC)

    # An unpaid purchase blocks the next one, but only while it is still live.
    #
    # It used to block for ever: `PENDING_PAYMENT` was set here and cleared only
    # by `activate_subscription`, so a checkout that failed -- or that somebody
    # simply closed -- left a row nothing would ever remove, and no sweep looked
    # at it. One failed card payment locked that owner out of buying a
    # subscription at all, which is the thing that keeps their shops selling.
    # The sentence even told them to wait for it to lapse, and nothing made it.
    open_already = (
        db.execute(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.status == SubscriptionStatus.PENDING_PAYMENT,
            )
        )
        .scalars()
        .first()
    )
    if open_already is not None:
        if open_already.created_at is not None and (
            now - open_already.created_at
        ) <= PURCHASE_LIFETIME:
            raise Conflict(ALREADY_BUYING)

        # Cancelled rather than deleted: "they tried and did not finish" is a
        # different fact from "they never tried", and support needs both --
        # the same reason a failed payment is recorded instead of removed.
        open_already.status = SubscriptionStatus.CANCELLED
        db.add(open_already)
        db.flush()

    existing = active_subscription(db, user_id)
    quote = quote_subscription(
        db,
        user_id=user_id,
        plan=plan,
        duration_months=duration_months,
        # The rate this owner has already been promised, if any. Read from the
        # subscription rather than from an argument, so a route cannot pass one.
        negotiated_price=existing.negotiated_price if existing is not None else None,
    )

    subscription = Subscription(
        user_id=user_id,
        plan_id=plan.id,
        status=SubscriptionStatus.PENDING_PAYMENT,
        duration_months=quote.duration_months,
        monthly_price_charged=quote.monthly_price,
        discount_percent=quote.discount_percent,
        total_amount=quote.total,
        negotiated_price=existing.negotiated_price if existing is not None else None,
    )
    db.add(subscription)
    db.flush()
    return subscription


def activate_subscription(
    db: Session, subscription: Subscription, *, now: datetime | None = None
) -> Subscription:
    """Put a paid subscription in force, starting when the current term ends.

    Idempotent: a webhook and the browser's callback both settle the same
    payment and both arrive here, and the second must not extend the term
    again.
    """
    now = now or datetime.now(UTC)

    if subscription.status is SubscriptionStatus.ACTIVE:
        return subscription

    if subscription.status is not SubscriptionStatus.PENDING_PAYMENT:
        raise Conflict(NOT_PURCHASABLE)

    if subscription.duration_months < 1:
        raise BadRequest("A subscription must run for at least a month.")

    # Extend from the end of what the owner has actually been given: the paid
    # term, or a trial running past it. **Not** the grace window -- grace is a
    # buffer so a shop does not go dark while a renewal is late, not time
    # anybody bought. Counting it here would hand out five free days to every
    # owner who renews during it, and quietly move the anniversary each year.
    #
    # `None` means nothing is in force, so the term starts today rather than
    # being back-dated into days the shop spent closed.
    current = active_subscription(db, subscription.user_id)
    starts_at = now
    if current is not None and current.id != subscription.id:
        entitled_until = max(
            (d for d in (current.expires_at, current.free_until) if d is not None),
            default=None,
        )
        if entitled_until is not None and entitled_until > now:
            starts_at = entitled_until

    subscription.status = SubscriptionStatus.ACTIVE
    subscription.starts_at = starts_at
    subscription.expires_at = starts_at + timedelta(
        days=DAYS_PER_MONTH * subscription.duration_months
    )
    subscription.grace_ends_at = subscription.expires_at + timedelta(days=GRACE_DAYS)

    db.add(subscription)
    db.flush()
    return subscription
