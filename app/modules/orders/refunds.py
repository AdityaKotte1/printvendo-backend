"""What a refund means for the order it paid for.

`OrderState.REFUNDED` existed as a value the enum could hold and nothing ever
set. That is the legacy audit's `REFUND_PENDING` exactly -- "written by one
line, read by nothing, accumulating real money owed" -- and a state nothing
reaches is worse than no state at all, because reports filter on it and find
nothing wrong.

The decision is derived from the two amounts, not passed in as an opinion: an
order is closed when everything paid for it has gone back, and not before. The
caller cannot ask for REFUNDED on a partial refund, because it does not get to
ask.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import as_money
from app.modules.orders.models import Order, OrderState


def apply_payment_refund(
    db: Session,
    *,
    order_id: int | None,
    refunded_inr: Decimal,
    paid_inr: Decimal,
    now: datetime | None = None,
) -> Order | None:
    """Close the order if all of its money has gone back.

    Returns None when there is no order to close. A wallet top-up and a
    subscription are Payments with no order, and refunding one must not raise on
    the way past -- so "nothing to do" is an answer, not an exception.

    A full refund closes the order **from any state**, printed or not. REFUNDED
    is a statement about the money and it is true whether or not paper came out;
    what printed is on the PrintTask rows, which is where anyone asking that
    question should be looking. Collapsing the two into one field is how the old
    backend ended up with a status that meant different things to each reader.
    """
    if order_id is None:
        return None

    order = db.execute(select(Order).where(Order.id == order_id)).scalar_one_or_none()
    if order is None:
        return None

    if as_money(refunded_inr) < as_money(paid_inr):
        # Partial. The student still has tasks queued and the operator must not
        # be told the order is finished.
        return order

    order.state = OrderState.REFUNDED
    order.refunded_at = now or datetime.now(UTC)
    db.add(order)
    db.flush()
    return order
