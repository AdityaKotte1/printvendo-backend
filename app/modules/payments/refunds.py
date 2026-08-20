"""Money given back, in whole or in part.

Two rules carry this module.

**The destination is forced by the account that collected**, and that account is
read off the Payment row rather than re-derived. `collecting_user_id` is the
gate's answer recorded at checkout; the owner whose account took the money six
months ago is the one that must refund it, whatever the kiosk looks like now.
Re-deriving it is how a refund ends up leaving the wrong account -- the backend
being replaced had `refunds` as one of three services independently deciding
whose Razorpay collects. Platform money may go back to the card or to the
student's wallet; an owner's money can only go back to the card, because the
platform cannot credit a balance against rupees it never held.

**A retried refund is not a second refund.** The caller supplies an idempotency
key and it is looked up *first*, before any amount or state check. That order
matters: a fully-refunded payment is REFUNDED with nothing left to give back, so
a retry validated first would be refused for exceeding the captured amount --
the caller would see a failure for a refund that had in fact succeeded, and
would very likely issue another with a fresh key. The key is also handed to
Razorpay for the to-source leg, so our idea of "already done" and theirs agree.

Payments does not import orders. A refund may mean something to an order -- a
fully-refunded print order becomes REFUNDED -- and orders already imports this
module, so the order transition comes back in through `RefundSink`. The
composition root wires it; the payment module only decides where money goes.
"""

from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.core.money import as_money, to_paise
from app.modules.payments.charges import RazorpayGateway
from app.modules.payments.models import (
    Payment,
    PaymentStatus,
    Refund,
    RefundDestination,
)
from app.modules.wallet import EntryKind, credit

AMOUNT_MUST_BE_POSITIVE = "A refund amount must be more than zero."
REFUND_EXCEEDS_CAPTURED = "That refund is more than what was paid."
ILLEGAL_DESTINATION = "Refunds from an owner's account must go back to the source."
NOTHING_TO_REVERSE = "That payment came from a balance, so it can only go back to one."
NO_GATEWAY = "Refunding to the source needs a live Razorpay connection."
ALREADY_REFUNDED = "That refund has already been recorded."
KEY_BELONGS_ELSEWHERE = "That refund reference has already been used for another payment."
PAYMENT_NOT_CAPTURED = "Only a payment that was made can be refunded."

REFUNDABLE = (PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED)


class RefundSink(Protocol):
    """What a refund means for whatever the payment was for.

    At the composition root this asks the orders module to close an order. The
    protocol exists so payments stays independent of orders, and so a test can
    watch a refund without an order existing at all.
    """

    def on_refund(self, db: Session, payment: Payment, refund: Refund) -> None: ...


def _may_go_to_wallet(payment: Payment) -> bool:
    """Whether this payment's money may be credited as platform balance.

    Only when nobody else collected it. A wallet credit is the platform
    promising to honour rupees it holds; against money that went straight to an
    owner's Razorpay account there is nothing behind that promise.
    """
    return payment.collecting_user_id is None


def _may_go_to_source(payment: Payment) -> bool:
    """Whether there is a gateway payment to reverse.

    Derived from the absence of a captured Razorpay id rather than from a flag
    somebody sets, so it cannot disagree with the row it describes. A wallet
    payment has none -- nothing was captured at a gateway -- and calling
    Razorpay with `razorpay_payment_id=None` is what this refuses.
    """
    return payment.razorpay_payment_id is not None


# The whole destination rule, as a table rather than a paragraph. Both columns
# are read off the Payment row, so a kiosk that changed hands or lost its keys
# since cannot move a refund to a different account.
#
#   paid by          collected by   -> wallet   -> source
#   wallet balance   nobody            yes        no: nothing to reverse
#   gateway          the platform      yes        yes
#   gateway          an owner          no         yes


def refund_for_key(db: Session, idempotency_key: str) -> Refund | None:
    """The refund this key already produced, if any."""
    return db.execute(
        select(Refund).where(Refund.idempotency_key == idempotency_key)
    ).scalar_one_or_none()


def refund_for_razorpay_id(db: Session, razorpay_refund_id: str) -> Refund | None:
    """The refund we already hold for Razorpay's id, if any.

    What makes a redelivered `refund.processed` a no-op, and what recognises our
    own to-source refund coming back to us as an event. The column is unique, so
    this is a lookup on the same constraint that would refuse the insert.
    """
    return db.execute(
        select(Refund).where(Refund.razorpay_refund_id == razorpay_refund_id)
    ).scalar_one_or_none()


def refunds_for(db: Session, payment: Payment) -> list[Refund]:
    """Everything given back against this payment, oldest first."""
    return list(
        db.execute(
            select(Refund)
            .where(Refund.payment_id == payment.id)
            .order_by(Refund.created_at, Refund.id)
        ).scalars()
    )


def refund(
    db: Session,
    *,
    payment: Payment,
    amount: Decimal,
    destination: RefundDestination,
    idempotency_key: str,
    actor_user_id: int | None = None,
    reason: str | None = None,
    razorpay: RazorpayGateway | None = None,
    external_refund_id: str | None = None,
    sink: RefundSink | None = None,
) -> Refund:
    """Issue a refund, or hand back the one this key already produced.

    Reusing a key with a different amount is a caller mistake, and the safe
    answer is the refund that key already made -- not a second movement of
    money. Reusing it against a *different payment* is refused, because
    returning someone else's refund row would report success for a refund that
    never happened.

    `external_refund_id` records a to-source refund that **already happened
    elsewhere** -- an owner refunding from their own Razorpay dashboard, which
    our books would otherwise never hear about. The gateway is not called, since
    calling it would issue a second refund for money already returned. Every
    other rule still applies, including the amount check: an event claiming more
    than was captured is refused rather than written.
    """
    already = refund_for_key(db, idempotency_key)
    if already is not None:
        if already.payment_id != payment.id:
            raise Conflict(KEY_BELONGS_ELSEWHERE)
        return already

    amount = as_money(amount)
    if amount <= Decimal("0.00"):
        raise BadRequest(AMOUNT_MUST_BE_POSITIVE)

    if payment.status not in REFUNDABLE:
        raise Conflict(PAYMENT_NOT_CAPTURED)

    if destination is RefundDestination.WALLET and not _may_go_to_wallet(payment):
        raise BadRequest(ILLEGAL_DESTINATION)
    if destination is RefundDestination.SOURCE and not _may_go_to_source(payment):
        raise BadRequest(NOTHING_TO_REVERSE)

    available = as_money(payment.amount_inr) - as_money(payment.refunded_inr)
    if amount > available:
        raise Conflict(REFUND_EXCEEDS_CAPTURED)

    razorpay_refund_id: str | None = external_refund_id
    if destination is RefundDestination.SOURCE and external_refund_id is None:
        if razorpay is None:
            # Fail closed. Writing the row without calling Razorpay would record
            # a refund the student never receives, and nothing would retry it.
            raise Conflict(NO_GATEWAY)
        razorpay_refund_id = razorpay.refund(
            razorpay_payment_id=payment.razorpay_payment_id,
            amount_paise=to_paise(amount),
            idempotency_key=idempotency_key,
        )

    refund_row = Refund(
        payment_id=payment.id,
        amount_inr=amount,
        destination=destination,
        idempotency_key=idempotency_key,
        razorpay_refund_id=razorpay_refund_id,
        actor_user_id=actor_user_id,
        reason=(reason or "").strip()[:300] or None,
    )
    try:
        # Inside a savepoint, for the reason the wallet ledger already documents:
        # an IntegrityError from flush() aborts the whole transaction, not just
        # the failed INSERT. Without this, catching it and raising a tidy
        # Conflict would hand the caller a session on which every later
        # statement fails with "current transaction is aborted", a long way from
        # the cause. Rolling back to the savepoint undoes exactly this refund.
        with db.begin_nested():
            db.add(refund_row)
            db.flush()
    except IntegrityError as exc:
        # Two callers with one key arrived together and both got past the
        # lookup, or two of our rows are claiming one Razorpay refund. The
        # unique index decides, rather than the SELECT above -- which is the
        # read-check-write that two concurrent retries both pass.
        raise Conflict(ALREADY_REFUNDED) from exc

    payment.refunded_inr = as_money(payment.refunded_inr + amount)
    payment.status = (
        PaymentStatus.REFUNDED
        if payment.refunded_inr >= as_money(payment.amount_inr)
        else PaymentStatus.PARTIALLY_REFUNDED
    )
    db.add(payment)

    if destination is RefundDestination.WALLET:
        # Reference is the idempotency key, so the ledger's own uniqueness is a
        # second guard on the same fact rather than a different one.
        credit(
            db,
            user_id=payment.user_id,
            amount=amount,
            kind=EntryKind.REFUND,
            reference=f"refund:{idempotency_key}",
            note=reason,
        )

    if sink is not None:
        sink.on_refund(db, payment, refund_row)

    db.flush()
    return refund_row
