"""Giving money back, from the admin surface.

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

This route is one of **two doors onto `app.refunding`**; the other is the
owner's, at their own shop. The difference between them is which orders are
reachable and nothing else. The old backend wrote the refund twice instead, and
the two copies disagreed about whose Razorpay account collects.
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
from app.modules.orders import Order, order_by_public_id
from app.modules.payments import RazorpayGateway, RefundSink
from app.refunding import refund_an_order

router = APIRouter(prefix="/v1/admin/orders", tags=["admin"])

CurrentAdmin = Annotated[User, Depends(require_role(Role.ADMIN))]

NO_SUCH_ORDER = "That order does not exist."


def _order(db: Session, order_id: str) -> Order:
    try:
        parse_id(order_id, IdPrefix.ORDER)
    except InvalidId:
        raise NotFound(NO_SUCH_ORDER) from None

    order = order_by_public_id(db, order_id)
    if order is None:
        raise NotFound(NO_SUCH_ORDER)
    return order


@router.post(
    "/{order_id}/refund",
    response_model=RefundResponse,
    status_code=status.HTTP_201_CREATED,
)
def refund_any_order(
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

    Every order in the estate is reachable here; that is the whole difference
    from the owner's door. The money itself moves through the same use case.
    """
    issued = refund_an_order(
        db,
        order=_order(db, order_id),
        actor_user_id=admin.id,
        idempotency_key=body.idempotency_key,
        amount_inr=body.amount_inr,
        destination=body.destination,
        reason=body.reason,
        razorpay=razorpay,
        box=box,
        sink=sink,
        platform_key_id=settings.RAZORPAY_KEY_ID,
        platform_key_secret=settings.RAZORPAY_KEY_SECRET,
    )
    return RefundResponse(**vars(issued))
