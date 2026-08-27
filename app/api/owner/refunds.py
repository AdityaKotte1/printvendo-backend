"""An owner giving money back at their own shop.

The complaint arrives at the counter: "it did not come out". Until this route
existed the only refund was admin-only, so the person standing in front of the
student had to email somebody and the student went home unrefunded.

It is **the same refund** as the admin's -- `app.refunding` -- and the two doors
differ only in which orders they reach and whose money may move. That is
deliberate: the old backend had a refund in `kiosk.py` for an owner and a
second in `refunds.py` for an admin, each independently deciding whose Razorpay
collects, and two answers to that question is how student money went to the
wrong account.

**A shop gives back money its own account collected.** One check --
`own_takings_only` -- and two consequences that are never enforced a second
time: the money comes back out of the *owner's* Razorpay, because
`credentials_for_payment` reads the same column; and it can only go to the
source, because a balance refund is legal only where nobody else collected.
`OwnerRefundRequest` therefore has no destination field at all: the question
cannot be asked, so it cannot be answered wrongly.

The kiosk is in the path and is checked, rather than being decoration. An order
that belonged to whichever kiosk happened to be named would make the resolver's
answer irrelevant; an order at one of the caller's *other* shops is a 404 here
exactly as a stranger's is.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    KioskScope,
    get_db,
    get_razorpay,
    get_refund_sink,
    get_secret_box,
    get_settings_from_app,
    require_role,
)
from app.api.schemas import OwnerRefundRequest, RefundResponse
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.identity.roles import Role
from app.modules.kiosks import repository as kiosk_repo
from app.modules.orders import Order, order_by_public_id
from app.modules.payments import RazorpayGateway, RefundSink
from app.refunding import refund_an_order

# OWNER alone, and this is the one place in the owner surface where admin is
# *not* alongside. Everywhere else admin is a wider kiosk scope through the
# same route; here the route is not about scope at all. It is "the shop gives
# back money it took", and an admin is not the shop -- their `collecting_user_id`
# is nobody's, so the rule below could only ever refuse them. Refusing at the
# door with a 403 says that; refusing at the money with a 409 would read as a
# bug. Platform money is given back through `/v1/admin/orders/{id}/refund`,
# which is what that door is for.
router = APIRouter(
    prefix="/v1/owner/kiosks",
    tags=["owner"],
    dependencies=[Depends(require_role(Role.OWNER))],
)

NO_SUCH_ORDER = "That order does not exist."


def _order_at(db: Session, kiosk_id: int, order_id: str) -> Order:
    """This shop's order, or nothing.

    One sentence for every way of missing: a malformed id, an order that never
    existed, and an order printed somewhere else. A caller who can tell those
    apart can walk the order space of a shop they do not hold.
    """
    try:
        parse_id(order_id, IdPrefix.ORDER)
    except InvalidId:
        raise NotFound(NO_SUCH_ORDER) from None

    order = order_by_public_id(db, order_id)
    if order is None or order.kiosk_id != kiosk_id:
        raise NotFound(NO_SUCH_ORDER)
    return order


@router.post(
    "/{kiosk_id}/orders/{order_id}/refund",
    response_model=RefundResponse,
    status_code=status.HTTP_201_CREATED,
)
def refund_an_order_here(
    kiosk_id: str,
    order_id: str,
    body: OwnerRefundRequest,
    actor: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    razorpay: Annotated[RazorpayGateway, Depends(get_razorpay)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
    sink: Annotated[RefundSink, Depends(get_refund_sink)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> RefundResponse:
    """Give back what a student paid at this shop.

    `idempotency_key` is required rather than generated here, and that is the
    whole safety of the thing: a request that times out on a shop's phone is
    retried with the same key and returns the refund it already made, instead
    of sending the money twice.

    It goes back out of your own Razorpay account, to the card or UPI it came
    from. That is not a choice this route offers: money collected into a shop's
    own account is the only money a shop may give back, and it can only go back
    the way it arrived. Takings Printvendo collected -- every PLATFORM kiosk,
    and every order paid from a balance -- are refused here with a sentence
    saying who to ask.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    order = _order_at(db, kiosk.id, order_id)

    issued = refund_an_order(
        db,
        order=order,
        actor_user_id=actor.id,
        idempotency_key=body.idempotency_key,
        amount_inr=body.amount_inr,
        reason=body.reason,
        razorpay=razorpay,
        box=box,
        sink=sink,
        platform_key_id=settings.RAZORPAY_KEY_ID,
        platform_key_secret=settings.RAZORPAY_KEY_SECRET,
        own_takings_only=True,
    )
    return RefundResponse(**vars(issued))
