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
from app.api.schemas import (
    AdminOrderResponse,
    OrderItemResponse,
    OrderPaymentResponse,
    OrderRefundResponse,
    OrderStudentResponse,
    RefundRequest,
    RefundResponse,
)
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.identity import User
from app.modules.identity import repository as identity_repo
from app.modules.identity.roles import Role
from app.modules.orders import Order, order_by_public_id, view_of
from app.modules.payments import (
    RazorpayGateway,
    RefundSink,
    payment_for_order,
    refunds_for,
)
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


NO_SUCH_STUDENT = "That order names an account that no longer exists."


@router.get("/{order_id}", response_model=AdminOrderResponse)
def read_an_order(
    order_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> AdminOrderResponse:
    """One order, whole.

    **Admin only, and that is the control rather than scope.** Everywhere else
    in this system an admin is a wider kiosk scope through the same route; here
    the route itself is different, because the owner surface is built to be
    *incapable* of carrying student identity. Giving an owner this view at a
    shop they hold would hand them exactly what `OwnerOrderResponse` exists to
    withhold, so the audience is what decides, not the kiosk.

    Three things live here and nowhere else: who paid, how the money actually
    moved, and what has already been given back. An operator answering "I was
    charged twice and nothing came out" needs all three in one place, and the
    order row alone cannot tell them whether a partial refund has been made.
    """
    order = _order(db, order_id)
    view = view_of(db, order)

    student = identity_repo.actors_by_id(db, {order.user_id}).get(order.user_id)
    if student is None:
        # A deleted account is not an error worth a 500 -- but it is not
        # something to render as a blank student either.
        raise NotFound(NO_SUCH_STUDENT)

    payment = payment_for_order(db, order.id)

    return AdminOrderResponse(
        id=view.id,
        kiosk_id=view.kiosk_id,
        kiosk_name=view.kiosk_name,
        state=view.state.value,
        payment_method=view.payment_method,
        subtotal_inr=view.subtotal_inr,
        fee_inr=view.fee_inr,
        total_inr=view.total_inr,
        created_at=view.created_at,
        paid_at=view.paid_at,
        refunded_at=view.refunded_at,
        expires_at=view.expires_at,
        student=OrderStudentResponse(
            id=student.public_id,
            email=student.email,
            full_name=student.full_name,
        ),
        items=[
            OrderItemResponse(
                document_id=item.document_id,
                filename=item.filename,
                kind=item.kind,
                colour=item.colour,
                duplex=item.duplex,
                copies=item.copies,
                page_range=item.page_range,
                page_count=item.page_count,
                sheets=item.sheets,
                amount_inr=item.amount_inr,
            )
            for item in view.items
        ],
        payment=None
        if payment is None
        else OrderPaymentResponse(
            id=payment.public_id,
            source=payment.source.value,
            status=payment.status.value,
            amount_inr=payment.amount_inr,
            refunded_inr=payment.refunded_inr,
            razorpay_order_id=payment.razorpay_order_id,
            razorpay_payment_id=payment.razorpay_payment_id,
            collected_by=(
                None
                if payment.collecting_user_id is None
                else _public_id_of(db, payment.collecting_user_id)
            ),
            created_at=payment.created_at,
            captured_at=payment.captured_at,
        ),
        refunds=[]
        if payment is None
        else [
            OrderRefundResponse(
                id=issued.public_id,
                amount_inr=issued.amount_inr,
                destination=issued.destination.value,
                reason=issued.reason,
                created_at=issued.created_at,
            )
            for issued in refunds_for(db, payment)
        ],
    )


def _public_id_of(db: Session, user_id: int) -> str | None:
    """The opaque id of whoever collected, never the numeric one.

    Numeric primary keys do not leave the database, so an operator reading this
    can hand it straight to `/v1/admin/accounts/{id}` without translating.
    """
    actor = identity_repo.actors_by_id(db, {user_id}).get(user_id)
    return actor.public_id if actor is not None else None
