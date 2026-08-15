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

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.billing.models import Subscription, SubscriptionStatus


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
