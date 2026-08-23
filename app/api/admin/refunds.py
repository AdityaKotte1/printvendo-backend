"""Giving money back.

`payments.refunds` has been built, and mutation-tested, since the payments
module landed -- and nothing exposed it. The first student charged for a print
that jammed could not be refunded at all, which is the one thing an operator
must be able to do the day real money starts moving.

**Refunds are issued against an order.** A complaint arrives as "my print did
not come out", which an operator can find; nobody has a payment id to hand. The
order names its payment, and from there every decision is read off the *payment*
-- never the kiosk, never the order -- because `collecting_user_id` is the
payment gate's answer recorded at checkout, and a kiosk's owner or keys may have
changed since.

The destination is derived, not chosen. A wallet payment has no gateway payment
to reverse, so balance is the only place it can go. A gateway payment goes back
the way it came unless somebody asks for balance, which is legal only where the
platform collected. The refund service enforces all of that; this route does not
restate it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    get_razorpay,
    get_refund_sink,
    get_secret_box,
    get_settings_from_app,
    require_role,
)
from app.api.schemas import RefundRequest, RefundResponse
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.identity import User
from app.modules.identity.roles import Role
from app.modules.ops import audit
from app.modules.orders import Order, order_by_public_id
from app.modules.payments import (
    Payment,
    PaymentSource,
    RazorpayGateway,
    RefundDestination,
    RefundSink,
    credentials_for_payment,
    payment_for_order,
    refund,
)

router = APIRouter(prefix="/v1/admin/orders", tags=["admin"])

CurrentAdmin = Annotated[User, Depends(require_role(Role.ADMIN))]

NO_SUCH_ORDER = "That order does not exist."
NOTHING_PAID = "Nothing has been paid for that order, so there is nothing to give back."


def _order(db: Session, order_id: str) -> Order:
    try:
        parse_id(order_id, IdPrefix.ORDER)
    except InvalidId:
        raise NotFound(NO_SUCH_ORDER) from None

    order = order_by_public_id(db, order_id)
    if order is None:
        raise NotFound(NO_SUCH_ORDER)
    return order


def _destination(payment: Payment, asked: str | None) -> RefundDestination:
    """Where the money goes, derived from how it arrived.

    A wallet payment never touched a gateway, so there is nothing to reverse and
    balance is the only legal answer -- asking for source would be refused by
    the refund service anyway, and refusing here would only duplicate the rule.
    """
    if asked is not None:
        return RefundDestination(asked)
    if payment.source is PaymentSource.WALLET:
        return RefundDestination.WALLET
    return RefundDestination.SOURCE


@router.post(
    "/{order_id}/refund",
    response_model=RefundResponse,
    status_code=status.HTTP_201_CREATED,
)
def refund_an_order(
    order_id: str,
    body: RefundRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    razorpay: Annotated[RazorpayGateway, Depends(get_razorpay)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
    sink: Annotated[RefundSink, Depends(get_refund_sink)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> RefundResponse:
    """Give back what was paid for an order, in full or in part.

    `idempotency_key` is required rather than generated here, and that is the
    whole safety of the thing: a request that times out is retried with the same
    key and returns the refund it already made, instead of sending the money
    twice. The same key is passed to Razorpay, so both sides agree on "done".

    Defaults to everything still owed, which is the case by a wide margin -- the
    print did not come out, so all of it goes back.
    """
    order = _order(db, order_id)

    payment = payment_for_order(db, order.id)
    if payment is None:
        raise NotFound(NOTHING_PAID)

    amount = body.amount_inr
    if amount is None:
        amount = payment.amount_inr - payment.refunded_inr

    destination = _destination(payment, body.destination)

    # Only a to-source refund needs an account to act on. Asking for the
    # credentials of a wallet payment is refused outright -- rightly, since it
    # never went through a gateway -- so a balance refund must not ask.
    credentials = None
    if destination is RefundDestination.SOURCE:
        # The collecting account's keys, read off the payment. An account can
        # only refund a payment it took, so these are never the gateway
        # object's own.
        credentials = credentials_for_payment(
            db,
            payment,
            box=box,
            platform_key_id=settings.RAZORPAY_KEY_ID,
            platform_key_secret=settings.RAZORPAY_KEY_SECRET,
        )

    issued = refund(
        db,
        payment=payment,
        amount=amount,
        destination=destination,
        idempotency_key=body.idempotency_key,
        actor_user_id=admin.id,
        reason=body.reason,
        razorpay=razorpay,
        credentials=credentials,
        sink=sink,
    )

    audit.record(
        db,
        action="payment.refunded",
        entity_type="payment",
        entity_id=payment.public_id,
        actor_user_id=admin.id,
        after={
            "refund_id": issued.public_id,
            "amount_inr": str(issued.amount_inr),
            "destination": RefundDestination(destination).value,
            "order_id": order.public_id,
        },
        note=body.reason,
    )

    return RefundResponse(
        id=issued.public_id,
        payment_id=payment.public_id,
        order_id=order.public_id,
        amount_inr=issued.amount_inr,
        destination=RefundDestination(destination).value,
        refunded_total_inr=payment.refunded_inr,
        created_at=issued.created_at,
    )
