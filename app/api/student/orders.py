"""Placing an order, and paying for it.

The whole reason this backend exists is expressed here. In the old one a student
called `POST /wallet/hold`, and a *second* request enqueued the print — so a
crash, a closed tab or a failed request between the two left money taken and
nothing printed. That state is unreachable now: both payment paths end in
`orders.mark_paid`, which sets the order PAID and creates the print tasks in one
transaction, and `get_db` commits once at the end of the request.

Wallet and gateway are two branches *into* one commit, not two flows.

Nothing here prices anything. The server quoted the order when it was placed and
froze the number on the row; `total_inr` is what gets charged, and the web app's
own estimate stays an estimate. The old backend accepted a client-supplied
amount at the gateway.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    get_db,
    get_razorpay,
    get_secret_box,
    get_settings_from_app,
)
from app.api.schemas import (
    CheckoutResponse,
    OrderItemResponse,
    OrderResponse,
    PlaceOrderRequest,
    VerifyPaymentRequest,
)
from app.api.student.kiosks import UNRESTRICTED
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import BadRequest, Conflict, NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.kiosks import repository as kiosk_repo
from app.modules.orders import (
    Order,
    OrderState,
    OrderView,
    PaymentMethod,
    RequestedDocument,
    mark_paid,
    order_for,
    orders_of,
    pay_with_wallet,
    place_order,
    render_invoice,
    view_of,
)
from app.modules.payments import (
    PaymentKind,
    RazorpayGateway,
    confirm_payment,
    credentials_for,
    open_checkout,
    payment_for_razorpay_order,
)
from app.modules.printing import PrintOptions
from app.modules.printing import repository as printing_repo

router = APIRouter(prefix="/v1/app/orders", tags=["student"])

NO_SUCH_ORDER = "That order does not exist."
NOT_A_GATEWAY_ORDER = "That order is not being paid by card."
ALREADY_PAID = "That order has already been paid for."
NO_CHECKOUT = "That order has no payment to confirm."


def _as_response(view: OrderView) -> OrderResponse:
    """Map the module's read model onto the wire shape.

    The public ids come from `orders.view_of`, not from ORM relationships:
    `Order` holds numeric keys into kiosks and printing, and a relationship
    across those boundaries would be an import of another context's table.
    """
    return OrderResponse(
        id=view.id,
        kiosk_id=view.kiosk_id,
        state=view.state.value,
        payment_method=view.payment_method,
        subtotal_inr=view.subtotal_inr,
        fee_inr=view.fee_inr,
        total_inr=view.total_inr,
        expires_at=view.expires_at,
        paid_at=view.paid_at,
        refunded_at=view.refunded_at,
        created_at=view.created_at,
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
    )


def _order_or_404(db: Session, user, order_id: str) -> Order:
    """This student's order, or 404.

    Somebody else's order is 404 rather than 403, for the same reason a kiosk
    out of scope is: a 403 confirms the order exists, and an order id is a
    guessable-shaped thing to probe with.
    """
    try:
        parse_id(order_id, IdPrefix.ORDER)
    except InvalidId:
        raise NotFound(NO_SUCH_ORDER) from None

    order = order_for(db, user_id=user.id, public_id=order_id)
    if order is None:
        raise NotFound(NO_SUCH_ORDER)
    return order


@router.post("", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
def place(
    body: PlaceOrderRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    """Quote the work and open the order for payment.

    Everything that can refuse an order refuses it here, before any money moves:
    a closed kiosk, a document that is not this student's or not ready, wallet
    at a shop that collects its own takings, an empty order. The old backend
    charged first and discovered the tray afterwards.
    """
    kiosk = kiosk_repo.get_kiosk(db, UNRESTRICTED, body.kiosk_id)

    requests: list[RequestedDocument] = []
    for line in body.items:
        # Raises NotFound for a malformed id, a missing document and somebody
        # else's alike -- one sentence for all three, so this route cannot be
        # used to find out whose documents exist.
        document = printing_repo.document_for_user(
            db, user_id=user.id, public_id=line.document_id
        )

        requests.append(
            RequestedDocument(
                document=document,
                options=PrintOptions.create(
                    total_pages=document.page_count or 0,
                    colour=line.colour,
                    duplex=line.duplex,
                    copies=line.copies,
                    page_range=line.page_range,
                ),
            )
        )

    order = place_order(
        db,
        user=user,
        kiosk=kiosk,
        requests=requests,
        method=PaymentMethod(body.payment_method),
    )
    return _as_response(view_of(db, order))


@router.get("", response_model=list[OrderResponse])
def list_orders(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limit: int = 50,
) -> list[OrderResponse]:
    return [_as_response(v) for v in orders_of(db, user_id=user.id, limit=limit)]


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(
    order_id: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    return _as_response(view_of(db, _order_or_404(db, user, order_id)))


NOT_PAID_YET = "There is no receipt for this order yet, because it has not been paid for."


@router.get("/{order_id}/invoice")
def order_invoice(
    order_id: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """The receipt for a paid order, as a PDF.

    **Bytes from an authenticated route, never a URL.** The old backend minted a
    signed link and handed it to the browser; the same rule that governs a
    bank-detail proof governs this, and for the same reason -- a document that
    can be fetched without proving who you are is a document anybody can fetch.

    Only a paid order has one. An unpaid order is a quote, and a PDF headed
    "Total paid" against money nobody has taken is a thing to wave at a shop
    counter.
    """
    order = _order_or_404(db, user, order_id)
    if order.paid_at is None:
        raise Conflict(NOT_PAID_YET)

    pdf = render_invoice(view_of(db, order), student_email=user.email)

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            # Named by the order's own id: a downloads folder with six receipts
            # in it should say which is which, and the id is what support asks
            # for. `inline` so a phone opens it rather than silently saving it.
            "Content-Disposition": f'inline; filename="printvendo-{order.public_id}.pdf"'
        },
    )


@router.post("/{order_id}/pay/wallet", response_model=OrderResponse)
def pay_from_wallet(
    order_id: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> OrderResponse:
    """Debit the balance and queue the prints, in one commit.

    There is no second request. `pay_with_wallet` debits, records the payment
    and creates the print tasks together, so the "paid but never printed" state
    the old backend could reach does not exist here.
    """
    order = _order_or_404(db, user, order_id)
    pay_with_wallet(db, order)
    return _as_response(view_of(db, order))


@router.post("/{order_id}/checkout", response_model=CheckoutResponse)
def open_gateway_checkout(
    order_id: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    razorpay: Annotated[RazorpayGateway, Depends(get_razorpay)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> CheckoutResponse:
    """Open a Razorpay order against whichever account collects at this kiosk.

    The gate decides, once, and the answer is written onto the Payment row --
    which is what a refund reads months later, rather than re-deriving it from a
    kiosk whose keys may have changed.

    The Payment row is written before the student is sent to pay, so a capture
    arriving by webhook always has something to attach itself to.
    """
    order = _order_or_404(db, user, order_id)

    if order.payment_method is not PaymentMethod.GATEWAY:
        raise BadRequest(NOT_A_GATEWAY_ORDER)
    if order.state is not OrderState.AWAITING_PAYMENT:
        raise Conflict(ALREADY_PAID)

    # Orders has no relationship to Kiosk -- that would be an import of another
    # context's table -- so the composition root resolves the foreign key,
    # through the same scope every other kiosk read goes through.
    kiosk = kiosk_repo.kiosk_by_internal_id(db, UNRESTRICTED, order.kiosk_id)

    collection = credentials_for(
        db,
        kiosk,
        box=box,
        platform_key_id=settings.RAZORPAY_KEY_ID,
        platform_key_secret=settings.RAZORPAY_KEY_SECRET,
    )

    payment = open_checkout(
        db,
        razorpay,
        user_id=user.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=order.total_inr,
        receipt=order.public_id,
        kiosk=kiosk,
        credentials=collection.credentials,
        gateway=collection.gateway,
        collecting_user_id=collection.collecting_user_id,
        order_id=order.id,
    )

    return CheckoutResponse(
        razorpay_order_id=payment.razorpay_order_id,
        # Not a secret: it appears in the checkout the student is about to see.
        # The key *secret* has no field on this response and never leaves here.
        razorpay_key_id=collection.credentials.key_id,
        amount_inr=payment.amount_inr,
        order_id=order.public_id,
    )


@router.post("/{order_id}/verify", response_model=OrderResponse)
def verify(
    order_id: str,
    body: VerifyPaymentRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> OrderResponse:
    """The browser's callback, checked against the key that opened the order.

    A callback that does not verify changes nothing at all -- no status, no
    payment id, no print task -- so a forged one cannot advance an order towards
    being printed. This is the same capture the webhook performs; whichever
    arrives first wins and the second is refused by the unique payment id.
    """
    order = _order_or_404(db, user, order_id)

    payment = payment_for_razorpay_order(db, body.razorpay_order_id)
    if payment is None or payment.order_id != order.id:
        raise NotFound(NO_CHECKOUT)

    # Orders has no relationship to Kiosk -- that would be an import of another
    # context's table -- so the composition root resolves the foreign key,
    # through the same scope every other kiosk read goes through.
    kiosk = kiosk_repo.kiosk_by_internal_id(db, UNRESTRICTED, order.kiosk_id)

    collection = credentials_for(
        db,
        kiosk,
        box=box,
        platform_key_id=settings.RAZORPAY_KEY_ID,
        platform_key_secret=settings.RAZORPAY_KEY_SECRET,
    )

    confirm_payment(
        db,
        payment,
        razorpay_payment_id=body.razorpay_payment_id,
        signature=body.razorpay_signature,
        key_secret=collection.credentials.key_secret,
    )

    # The one commit: PAID and the print tasks, together.
    mark_paid(db, order, reference=body.razorpay_payment_id)
    return _as_response(view_of(db, order))
