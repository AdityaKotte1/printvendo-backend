"""Giving money back, once, for whoever is doing it.

Two people can refund an order: an admin, who reaches every order in the estate,
and the owner of the shop where it was printed, who reaches the ones at their
own kiosks. That is a difference about **which orders are visible**, and nothing
else -- the money moves the same way, lands in the same place, and leaves the
same trail.

The old backend had exactly this feature twice: a refund in `kiosk.py` for an
owner and another in `refunds.py` for an admin, each independently deciding
whose Razorpay account collects. Two answers to that question is how student
money reached the wrong account. So this is the one implementation, and the two
routes are doors onto it: they resolve an order and hand it here.

A use-case layer rather than a module, for the same reason `provisioning` is
one: it spans orders, payments and ops, which no bounded context may do. It sits
below the composition roots so both doors run the same code rather than two
copies that agree today.

**Nothing here decides where the money goes.** `payments.refunds` owns the
destination table, derived from two reads off the payment row, and this asks it
rather than restating it. What is decided here is only the *default*: back the
way it came, which is right in every case and is what an operator would type.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.crypto import SecretBox
from app.core.errors import Conflict, NotFound
from app.modules.ops import audit
from app.modules.orders import Order
from app.modules.payments import (
    Payment,
    PaymentSource,
    RazorpayGateway,
    RefundDestination,
    RefundSink,
    credentials_for_payment,
    payment_for_order,
    refund,
    refund_for_key,
)

NOTHING_PAID = "Nothing has been paid for that order, so there is nothing to give back."

FOREIGN_MONEY = (
    "That payment was collected by Printvendo rather than by your own "
    "account, so it cannot be refunded from here. Ask Printvendo to refund it."
)


@dataclass(frozen=True)
class IssuedRefund:
    """What was given back, and how much has been now in total.

    `refunded_total_inr` is the payment's running figure rather than this
    refund's amount, so a partial refund does not need a second call to be
    understood -- "have I already given half of this back" is the question
    somebody asks with a student in front of them.
    """

    id: str
    payment_id: str
    order_id: str
    amount_inr: Decimal
    destination: str
    refunded_total_inr: Decimal
    created_at: datetime


def _destination(payment: Payment, asked: str | None) -> RefundDestination:
    """Where the money goes when nobody said.

    Back the way it came: balance for a payment made from a balance, source for
    one made with a card. A wallet payment never touched a gateway, so there is
    nothing to reverse -- asking for source is refused by the refund service,
    and refusing it here as well would be a second copy of that rule.
    """
    if asked is not None:
        return RefundDestination(asked)
    if payment.source is PaymentSource.WALLET:
        return RefundDestination.WALLET
    return RefundDestination.SOURCE


def refund_an_order(
    db: Session,
    *,
    order: Order,
    actor_user_id: int,
    idempotency_key: str,
    amount_inr: Decimal | None = None,
    destination: str | None = None,
    reason: str | None = None,
    razorpay: RazorpayGateway,
    box: SecretBox,
    sink: RefundSink,
    platform_key_id: str,
    platform_key_secret: str,
    own_takings_only: bool = False,
) -> IssuedRefund:
    """Give back what was paid for this order, in full or in part.

    The caller has already decided that this actor may see this order. What is
    left is money, and every decision about it is read off the **payment** --
    never the kiosk, never the order -- because `collecting_user_id` is the
    payment gate's answer recorded at checkout, and a kiosk's owner or keys may
    have changed since.

    `amount_inr` omitted means everything still owed, which is the case by a
    wide margin: the print did not come out, so all of it goes back.

    `own_takings_only` is the owner door's constraint: **you give back money
    your own account collected.** It is one check with two consequences, and
    neither is enforced a second time anywhere:

    * the money necessarily comes back out of the *owner's* Razorpay, because
      `credentials_for_payment` reads the same `collecting_user_id` this test
      reads. One column, so the two cannot disagree;
    * it can only go to the source, because a wallet refund is legal only when
      `collecting_user_id is None` -- which this has just refused.

    Admin passes it false. That is not a bypass: platform money is the
    platform's to give back, and refunding it is exactly what the admin door is
    for.
    """
    payment = payment_for_order(db, order.id)
    if payment is None:
        raise NotFound(NOTHING_PAID)

    if own_takings_only and payment.collecting_user_id != actor_user_id:
        # A wallet payment lands here too, and correctly: it has no collecting
        # account at all, so there is nothing in the shop's Razorpay to reverse.
        raise Conflict(FOREIGN_MONEY)

    if amount_inr is None:
        amount_inr = payment.amount_inr - payment.refunded_inr

    where = _destination(payment, destination)

    # Only a to-source refund needs an account to act on. Asking for the
    # credentials of a wallet payment is refused outright -- rightly, since it
    # never went through a gateway -- so a balance refund must not ask.
    credentials = None
    if where is RefundDestination.SOURCE:
        # The collecting account's keys, read off the payment. An account can
        # only refund a payment it took, so these are never the platform's by
        # default.
        credentials = credentials_for_payment(
            db,
            payment,
            box=box,
            platform_key_id=platform_key_id,
            platform_key_secret=platform_key_secret,
        )

    # Asked *before* the refund, because afterwards there is no way to tell a
    # retry from a first attempt: `refund` returns the existing row either way,
    # which is exactly what makes it safe to retry.
    already_done = refund_for_key(db, idempotency_key) is not None

    issued = refund(
        db,
        payment=payment,
        amount=amount_inr,
        destination=where,
        idempotency_key=idempotency_key,
        actor_user_id=actor_user_id,
        reason=reason,
        razorpay=razorpay,
        credentials=credentials,
        sink=sink,
    )

    # One refund, one entry. A retry gives back the refund that already
    # happened and must not gain a second line in the trail: an operator
    # reading two entries of a hundred rupees where one refund was made has no
    # way to tell which is true, and this trail is the only record there is --
    # owners are paid directly, so there is no settlement run in which the
    # discrepancy would surface.
    if not already_done:
        audit.record(
            db,
            action="payment.refunded",
            entity_type="payment",
            entity_id=payment.public_id,
            actor_user_id=actor_user_id,
            after={
                "refund_id": issued.public_id,
                "amount_inr": str(issued.amount_inr),
                "destination": where.value,
                "order_id": order.public_id,
            },
            note=reason,
        )

    return IssuedRefund(
        id=issued.public_id,
        payment_id=payment.public_id,
        order_id=order.public_id,
        amount_inr=issued.amount_inr,
        destination=where.value,
        refunded_total_inr=payment.refunded_inr,
        created_at=issued.created_at,
    )
