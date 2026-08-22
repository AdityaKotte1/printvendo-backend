"""What was taken, and what went back.

One query, one predicate. The backend being replaced computed revenue in three
places with two different ideas of which payment states count, so an owner's
earnings page and their kiosk page reported different numbers for the same day
and neither was wrong by its own definition. `SETTLED_PAYMENT_STATES` is that
predicate, defined once in `models.py` and imported rather than restated.

**Gross and refunded are separate numbers, and net is derived.** A refunded
payment is still money that arrived; treating a refund as "the sale never
happened" is how a full refund erases a day's takings from history instead of
showing as a sale and a refund. The old backend's `net_collected` clamped at
zero with `max(Decimal("0"), ...)`, which silently hid the case where refunds
exceeded takings -- exactly the case somebody needs to be told about.

Aggregated in SQL rather than by loading rows. A kiosk with a year of orders is
not something to pull into Python to add up, and the old backend's earnings
endpoint did precisely that.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.money import as_money
from app.modules.payments.models import (
    SETTLED_PAYMENT_STATES,
    Payment,
    PaymentKind,
    PaymentSource,
)

ZERO = Decimal("0.00")


@dataclass(frozen=True)
class Earnings:
    """Money in, money back, and the difference.

    `net` may be negative. That is a real state -- refunds issued today against
    takings from last week -- and reporting it as zero would hide the one
    situation an operator has to act on.
    """

    gross_inr: Decimal
    refunded_inr: Decimal
    net_inr: Decimal
    order_count: int

    @classmethod
    def of(cls, gross, refunded, count: int) -> "Earnings":
        gross = as_money(gross or ZERO)
        refunded = as_money(refunded or ZERO)
        return cls(
            gross_inr=gross,
            refunded_inr=refunded,
            net_inr=as_money(gross - refunded),
            order_count=count or 0,
        )


def _settled(stmt):
    """The one filter. Never re-expressed at a call site."""
    return stmt.where(Payment.status.in_(SETTLED_PAYMENT_STATES))


def earnings_for_kiosks(
    db: Session,
    kiosk_ids: list[int],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    include_wallet: bool = True,
) -> Earnings:
    """What these kiosks took over the window.

    `include_wallet` exists because the two are different money from an owner's
    point of view: a wallet payment was collected by Printvendo and is owed to
    the owner, while a gateway payment at an owner-collecting kiosk is already
    in the owner's own account. Reporting them as one number is fine for
    "what did this shop sell" and wrong for "what am I owed", so the caller
    says which question it is asking.
    """
    if not kiosk_ids:
        return Earnings.of(ZERO, ZERO, 0)

    stmt = _settled(
        select(
            func.coalesce(func.sum(Payment.amount_inr), 0),
            func.coalesce(func.sum(Payment.refunded_inr), 0),
            func.count(Payment.id),
        ).where(
            Payment.kiosk_id.in_(kiosk_ids),
            Payment.kind == PaymentKind.PRINT_ORDER,
        )
    )

    if not include_wallet:
        stmt = stmt.where(Payment.source != PaymentSource.WALLET)
    if since is not None:
        stmt = stmt.where(Payment.captured_at >= since)
    if until is not None:
        stmt = stmt.where(Payment.captured_at < until)

    gross, refunded, count = db.execute(stmt).one()
    return Earnings.of(gross, refunded, count)


def earnings_by_kiosk(
    db: Session,
    kiosk_ids: list[int],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[int, Earnings]:
    """The same figures, split per kiosk, in one query rather than N.

    An owner with twenty shops asking "how is each doing" is one round trip.
    Calling `earnings_for_kiosks` in a loop would be the N+1 that made the old
    backend's owner dashboard take seconds to load.
    """
    if not kiosk_ids:
        return {}

    stmt = _settled(
        select(
            Payment.kiosk_id,
            func.coalesce(func.sum(Payment.amount_inr), 0),
            func.coalesce(func.sum(Payment.refunded_inr), 0),
            func.count(Payment.id),
        ).where(
            Payment.kiosk_id.in_(kiosk_ids),
            Payment.kind == PaymentKind.PRINT_ORDER,
        )
    ).group_by(Payment.kiosk_id)

    if since is not None:
        stmt = stmt.where(Payment.captured_at >= since)
    if until is not None:
        stmt = stmt.where(Payment.captured_at < until)

    found = {
        kiosk_id: Earnings.of(gross, refunded, count)
        for kiosk_id, gross, refunded, count in db.execute(stmt).all()
    }
    # Every requested kiosk appears, including the ones that took nothing. A
    # missing key reads as "no data" and gets rendered as a blank; zero is the
    # true and more useful answer.
    return {kiosk_id: found.get(kiosk_id, Earnings.of(ZERO, ZERO, 0)) for kiosk_id in kiosk_ids}


@dataclass(frozen=True)
class PlatformRevenue:
    """What the platform itself took, in four buckets that are not addable.

    Deliberately not one number. They are four different kinds of money, and a
    total across them would be true of nothing:

    * `print_platform` -- print money that landed with us, whether through the
      platform's own Razorpay or out of a student's balance.
    * `print_owners` -- print money collected straight into an owner's account.
      It never touched us. Worth reporting, because it is what the estate turns
      over; adding it to our takings would claim money we have never held.
    * `subscriptions` -- what owners pay us for the software. Ours outright.
    * `wallet_topups` -- money we hold on somebody else's behalf. A liability,
      not income: counting it as takings would book a balance nobody has spent
      yet as revenue, and the figure would grow with every top-up.

    What is **not** here: how much of `print_platform` is owed onward to shop
    owners whose students paid from a balance. That is a question about who owns
    a kiosk, and payments does not know -- the answer belongs to something above
    both contexts. An approximation computed here would be a second, wrong
    opinion about settlement.
    """

    print_platform: Earnings
    print_owners: Earnings
    subscriptions: Earnings
    wallet_topups: Earnings


def platform_revenue(
    db: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> PlatformRevenue:
    """The four buckets over a window, in one query.

    Grouped in SQL by the two columns that distinguish them -- `kind`, and
    whether an owner collected -- rather than by running four aggregates. The
    same `SETTLED_PAYMENT_STATES` predicate as every other revenue figure in the
    system, so a platform total and an owner's own page can never disagree about
    which payments count.
    """
    collected_by_owner = Payment.collecting_user_id.is_not(None)

    stmt = _settled(
        select(
            Payment.kind,
            collected_by_owner.label("owner_collected"),
            func.coalesce(func.sum(Payment.amount_inr), 0),
            func.coalesce(func.sum(Payment.refunded_inr), 0),
            func.count(Payment.id),
        )
    ).group_by(Payment.kind, collected_by_owner)

    if since is not None:
        stmt = stmt.where(Payment.captured_at >= since)
    if until is not None:
        stmt = stmt.where(Payment.captured_at < until)

    buckets: dict[tuple[PaymentKind, bool], Earnings] = {
        (PaymentKind(kind), bool(owner_collected)): Earnings.of(gross, refunded, count)
        for kind, owner_collected, gross, refunded, count in db.execute(stmt).all()
    }

    def bucket(kind: PaymentKind, *, owner_collected: bool = False) -> Earnings:
        return buckets.get((kind, owner_collected), Earnings.of(ZERO, ZERO, 0))

    return PlatformRevenue(
        print_platform=bucket(PaymentKind.PRINT_ORDER),
        print_owners=bucket(PaymentKind.PRINT_ORDER, owner_collected=True),
        # A subscription is always collected by the platform: an owner does not
        # pay themselves for the software. Reading the platform bucket rather
        # than summing both is what would make a misrouted one visible as a
        # missing figure instead of being quietly folded in.
        subscriptions=bucket(PaymentKind.SUBSCRIPTION),
        wallet_topups=bucket(PaymentKind.WALLET_TOPUP),
    )
