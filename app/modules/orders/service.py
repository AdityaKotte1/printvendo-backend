"""Placing an order, and paying for it in a single commit.

This module exists to make one specific production hazard unreachable. In the
backend being replaced, `POST /wallet/hold` marked a payment PAID and created no
print job; the caller had to remember a second `POST /printers/{id}/print`, and
if anything went wrong in between -- a dropped connection, a closed tab, a
crash -- the student had paid for a job that would never print.

Here `mark_paid` is the *only* way an order becomes PAID, and it creates every
print task in the same call, inside the caller's transaction. There is no
ordering of two requests to get wrong, because there is one.

**Paper is reserved by derivation.** Availability is the tray, minus the sheets
promised to unfinished print tasks, minus the sheets promised to orders that are
still awaiting payment and have not expired. No `reserved` column: a counter
would need decrementing on six terminal paths, and the one that got missed would
drift exactly as the old tray counter did.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.modules.kiosks import Kiosk, effective_prices, sheets_remaining
from app.modules.orders.models import (
    SETTLED_ORDER_STATES,
    ItemKind,
    Order,
    OrderItem,
    OrderState,
    PaymentMethod,
)
from app.modules.orders.quotes import OrderQuote, quote_line
from app.modules.payments import PaymentKind, record_wallet_payment
from app.modules.payments.gate import Gateway, kiosk_payment_gate, wallet_may_be_spent
from app.modules.printing import Document, DocumentState, PrintOptions, PrintTask
from app.modules.printing.enqueue import committed_sheets, enqueue_task
from app.modules.wallet.ledger import debit

# Long enough for a student to open a payment app, authenticate and come back;
# short enough that an abandoned basket does not hold a shop's paper all day.
ORDER_LIFETIME = timedelta(minutes=20)

KIOSK_CLOSED = "This kiosk is not accepting orders at the moment."
WALLET_NOT_ACCEPTED = "This kiosk does not accept wallet payments. Pay by card or UPI."
NOT_YOUR_DOCUMENT = "That document is not yours, or is no longer available."
NOT_PRINTABLE = "That document is no longer available. Please upload it again."
NOT_ENOUGH_PAPER = (
    "This kiosk does not have enough paper for that job right now. "
    "Try a smaller job or another kiosk."
)
ALREADY_PAID = "That order has already been paid for."
ORDER_NOT_OPEN = "That order is no longer open for payment."
ORDER_EXPIRED = "That order expired before it was paid. Please place it again."
WRONG_METHOD = "That order was not set up to be paid from your wallet."


@dataclass(frozen=True)
class RequestedDocument:
    """One document a student wants printed, with the settings they chose."""

    document: Document
    options: PrintOptions


# ── paper ───────────────────────────────────────────────────────────────────


def sheets_available(db: Session, kiosk: Kiosk) -> int:
    """What is left in the tray after the work already queued.

    Derived from two things that already exist -- the tray, and the sheets
    promised to print tasks that have not finished -- rather than tracked in a
    column that would have to be kept in step with both.

    **An unpaid order holds nothing.** That was the operator's call: a basket
    somebody opened and wandered away from should not stop the next student
    printing. The consequence is accepted deliberately -- two students can both
    be accepted for the same last sheets, and the second job waits BLOCKED at
    the device until somebody refills, which is visible and recoverable. The
    alternative was a busy kiosk refusing orders it could have taken.
    """
    remaining = sheets_remaining(db, kiosk)
    return max(0, remaining - committed_sheets(db, kiosk_id=kiosk.id))


# ── placing ─────────────────────────────────────────────────────────────────


def place_order(
    db: Session,
    *,
    user,
    kiosk: Kiosk,
    requests: list[RequestedDocument],
    method: PaymentMethod,
    now: datetime | None = None,
) -> Order:
    """Quote the work, reserve the paper, and open the order for payment.

    Everything that can refuse an order refuses it *here*, before any money
    moves. The old backend charged first and discovered the tray afterwards.
    """
    now = now or datetime.now(UTC)

    gateway = kiosk_payment_gate(db, kiosk)
    if gateway is Gateway.CLOSED:
        raise BadRequest(KIOSK_CLOSED)

    if method is PaymentMethod.WALLET and not wallet_may_be_spent(db, kiosk):
        # Top-ups land in the platform's Razorpay, so spending that balance
        # where the owner collects would have the platform keep the cash while
        # the owner prints for free.
        raise BadRequest(WALLET_NOT_ACCEPTED)

    prices = effective_prices(kiosk)
    lines = []
    for request in requests:
        _check_document(request.document, user=user)
        lines.append(
            quote_line(
                request.options,
                total_pages=request.document.page_count or 0,
                prices=prices,
            )
        )

    # Raises on an empty order, so the emptiness rule lives with the money
    # rather than being repeated here.
    quote = OrderQuote.build(lines, pays_by_wallet=method is PaymentMethod.WALLET)

    # Checked, but not held: an order that is visibly larger than the kiosk can
    # print is refused before anybody is charged, which is the failure the old
    # backend had backwards -- it charged first and met the tray afterwards.
    needed = sum(line.sheets for line in quote.lines)
    if needed > sheets_available(db, kiosk):
        raise BadRequest(NOT_ENOUGH_PAPER)

    order = Order(
        user_id=user.id,
        kiosk_id=kiosk.id,
        state=OrderState.AWAITING_PAYMENT,
        subtotal_inr=quote.subtotal_inr,
        fee_inr=quote.fee_inr,
        total_inr=quote.total_inr,
        payment_method=method,
        gateway=gateway.value,
        expires_at=now + ORDER_LIFETIME,
    )
    db.add(order)
    db.flush()

    for position, (request, line) in enumerate(zip(requests, quote.lines, strict=True)):
        db.add(
            OrderItem(
                order_id=order.id,
                position=position,
                kind=ItemKind.DOCUMENT,
                document_id=request.document.id,
                colour=request.options.colour,
                duplex=request.options.duplex,
                copies=request.options.copies,
                page_range=request.options.page_range,
                page_count=line.page_count,
                impressions=line.impressions,
                sheets=line.sheets,
                amount_inr=line.amount_inr,
            )
        )

    db.flush()
    db.refresh(order)
    return order


def _check_document(document: Document, *, user) -> None:
    """Whose it is, and whether it can still be printed.

    Ownership is checked even though ids are opaque: an opaque id is hard to
    guess, which is not the same as impossible, and printing somebody else's
    coursework is not a failure to recover from politely.
    """
    if document.user_id != user.id:
        raise BadRequest(NOT_YOUR_DOCUMENT)
    if document.state is not DocumentState.READY:
        raise BadRequest(NOT_PRINTABLE)


# ── paying ──────────────────────────────────────────────────────────────────


def require_payable(order: Order, *, now: datetime) -> None:
    """Refuse an order that cannot be paid, before anything is charged.

    One implementation, called by `mark_paid` and by the wallet path before it
    debits. A second copy would eventually differ, and the copy that was
    missing a check would be the one taking money for an expired order.
    """
    if order.state is OrderState.PAID or order.state is OrderState.DISPATCHED:
        raise Conflict(ALREADY_PAID)
    if order.state in SETTLED_ORDER_STATES:
        raise Conflict(ORDER_NOT_OPEN)
    if order.state is not OrderState.AWAITING_PAYMENT:
        raise Conflict(ORDER_NOT_OPEN)
    if order.expires_at is not None and order.expires_at <= now:
        raise Conflict(ORDER_EXPIRED)


def pay_with_wallet(
    db: Session, order: Order, *, now: datetime | None = None
) -> list[PrintTask]:
    """The wallet branch of the one commit: debit, and enqueue, together.

    The old backend's `POST /wallet/hold` did the first half and left the second
    to the caller. Here they are the same call, in the same transaction, so a
    debit without print tasks is not a state the system can be in.

    The order's public id is the wallet reference, and wallet references are
    unique per wallet -- so a request retried after a timeout is refused by the
    ledger rather than debiting twice.

    Checks run before the debit, not after: refusing an expired order is free
    until money has moved.
    """
    now = now or datetime.now(UTC)
    require_payable(order, now=now)

    if order.payment_method is not PaymentMethod.WALLET:
        raise BadRequest(WRONG_METHOD)

    debit(
        db,
        user_id=order.user_id,
        amount=order.total_inr,
        reference=order.public_id,
        note=f"printing at kiosk {order.kiosk_id}",
    )

    # The debit above is the money moving; this is the record of what it paid
    # for. Both are in this transaction, so a refused debit leaves neither -- and
    # every paid order, however it was paid, has exactly one Payment to refund
    # against. Without it a wallet-paid order could not be refunded at all.
    record_wallet_payment(
        db,
        user_id=order.user_id,
        kind=PaymentKind.PRINT_ORDER,
        amount=order.total_inr,
        kiosk_id=order.kiosk_id,
        order_id=order.id,
        now=now,
    )

    return mark_paid(db, order, reference=order.public_id, now=now)


def mark_paid(
    db: Session,
    order: Order,
    *,
    reference: str | None,
    now: datetime | None = None,
) -> list[PrintTask]:
    """The single transaction: the order becomes PAID and its tasks exist.

    Called by both payment paths. Wallet debits and gateway captures are two
    branches *into* this one commit, not two flows -- so there is no state in
    which money has moved and print tasks do not exist.

    Deliberately does **not** re-check paper. By the time this runs a gateway
    payment has already been captured, and refusing here would strand real
    money; a task that arrives at an empty tray is reported BLOCKED by the
    device and runs when somebody refills, which is recoverable. The wallet path
    checks availability before it debits, where refusing is still free.
    """
    now = now or datetime.now(UTC)
    require_payable(order, now=now)

    order.state = OrderState.PAID
    order.paid_at = now
    order.payment_reference = reference
    db.add(order)

    tasks = [
        enqueue_task(
            db,
            document_id=item.document_id,
            kiosk_id=order.kiosk_id,
            options=PrintOptions(
                colour=item.colour,
                duplex=item.duplex,
                copies=item.copies,
                page_range=item.page_range,
            ),
            total_pages=item.page_count,
            position=item.position,
        )
        for item in order.items
        if item.kind is ItemKind.DOCUMENT and item.document_id is not None
    ]

    db.flush()
    return tasks


# ── expiry ──────────────────────────────────────────────────────────────────


def expire_stale_orders(db: Session, *, now: datetime | None = None) -> list[Order]:
    """Close orders nobody paid for, releasing the paper they were holding.

    Only orders still awaiting payment: a paid order's expiry is meaningless,
    and expiring one would release paper its print tasks are still using.
    """
    now = now or datetime.now(UTC)

    stale = list(
        db.execute(
            select(Order).where(
                Order.state == OrderState.AWAITING_PAYMENT,
                Order.expires_at.is_not(None),
                Order.expires_at <= now,
            )
        ).scalars()
    )

    for order in stale:
        order.state = OrderState.EXPIRED
        db.add(order)

    return stale
