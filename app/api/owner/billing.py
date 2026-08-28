"""What an owner is paying for, and how they pay for it.

This closes the hole that made the owner and SaaS models unworkable: an admin
could *grant* a trial and nothing could take money for a renewal, so when the
trial lapsed `kiosk_payment_gate` answered CLOSED and the shop stopped selling
with nothing the owner could do about it.

**A subscription is always collected by the platform**, never by the owner's own
Razorpay keys. It is our income, not theirs, and routing it through the account
they collect print takings into would have a shop paying itself.

The quote comes from `quote_subscription`, the one function that prices a
subscription, so what the owner is shown here and what the invoice says later
cannot disagree.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    get_db,
    get_razorpay,
    get_secret_box,
    get_settings_from_app,
    require_any_role,
)
from app.api.schemas import (
    CheckoutResponse,
    MyBillingResponse,
    MySubscriptionResponse,
    PlanResponse,
    StartSubscriptionRequest,
    SubscriptionQuoteResponse,
    VerifyPaymentRequest,
)
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import BadRequest, NotFound
from app.modules.billing import (
    InvoiceParty,
    Subscription,
    activate_subscription,
    active_plans,
    active_subscription,
    effective_end,
    invoice_number,
    is_on_trial,
    plan_by_public_id,
    plan_named,
    quote_subscription,
    render_subscription_invoice,
    start_purchase,
    subscription_by_public_id,
    subscriptions_of,
)
from app.modules.identity.roles import Role
from app.modules.payments import (
    Credentials,
    Gateway,
    PaymentKind,
    PaymentStatus,
    RazorpayGateway,
    confirm_payment,
    open_checkout,
    payment_for_razorpay_order,
    payment_for_subscription,
)

router = APIRouter(
    prefix="/v1/owner/billing",
    tags=["owner"],
    dependencies=[Depends(require_any_role(Role.OWNER, Role.ADMIN))],
)

NO_PLATFORM_KEYS = "Subscriptions cannot be bought right now. Please try again later."
NO_SUCH_PLAN = "That plan does not exist."
NO_SUCH_SUBSCRIPTION = "That subscription does not exist."
NO_CHECKOUT = "No payment was started for that subscription."


def _as_subscription(subscription: Subscription | None) -> MySubscriptionResponse | None:
    if subscription is None:
        return None
    return MySubscriptionResponse(
        id=subscription.public_id,
        status=subscription.status.value,
        duration_months=subscription.duration_months,
        monthly_price_charged=subscription.monthly_price_charged,
        total_amount=subscription.total_amount,
        on_trial=is_on_trial(subscription),
        free_until=subscription.free_until,
        starts_at=subscription.starts_at,
        expires_at=subscription.expires_at,
        # What an owner actually wants to know: the day the shop stops
        # collecting if they do nothing.
        covered_until=effective_end(subscription),
    )


@router.get("", response_model=MyBillingResponse)
def my_billing(
    owner: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> MyBillingResponse:
    """What is in force, and what is available to buy.

    Both together, because "am I covered" and "what would it cost to stay
    covered" are one question an owner asks at one moment.
    """
    return MyBillingResponse(
        subscription=_as_subscription(active_subscription(db, owner.id)),
        history=[
            row
            for row in (
                _as_subscription(s) for s in subscriptions_of(db, owner.id)
            )
            if row is not None
        ],
        plans=[
            PlanResponse(
                id=plan.public_id,
                name=plan.name,
                monthly_price=plan.monthly_price,
                max_kiosks=plan.max_kiosks,
                price_floor_bw=plan.price_floor_bw,
                price_ceiling_bw=plan.price_ceiling_bw,
                price_floor_color=plan.price_floor_color,
                price_ceiling_color=plan.price_ceiling_color,
                is_active=plan.is_active,
                discounts=[],
            )
            for plan in active_plans(db)
        ],
    )


@router.get("/quote", response_model=SubscriptionQuoteResponse)
def quote(
    plan_id: str,
    duration_months: int,
    owner: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> SubscriptionQuoteResponse:
    """What this owner would pay, with their own discounts applied.

    Read-only, and deliberately separate from opening a checkout: an owner
    comparing six months against twelve should not leave a trail of abandoned
    payments behind them.
    """
    plan = plan_by_public_id(db, plan_id)
    if plan is None:
        raise NotFound(NO_SUCH_PLAN)

    current = active_subscription(db, owner.id)
    priced = quote_subscription(
        db,
        user_id=owner.id,
        plan=plan,
        duration_months=duration_months,
        negotiated_price=current.negotiated_price if current is not None else None,
    )
    return SubscriptionQuoteResponse(
        plan_id=plan.public_id,
        plan_name=plan.name,
        duration_months=priced.duration_months,
        monthly_price=priced.monthly_price,
        discount_percent=priced.discount_percent,
        total=priced.total,
    )


@router.post("/subscription", response_model=CheckoutResponse, status_code=201)
def buy_subscription(
    body: StartSubscriptionRequest,
    owner: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    razorpay: Annotated[RazorpayGateway, Depends(get_razorpay)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> CheckoutResponse:
    """Open a Razorpay order for a subscription.

    Nothing is put in force here. The subscription is written PENDING_PAYMENT
    and activated when the capture arrives -- by webhook, or by the browser
    coming back -- because a subscription that counted from the moment somebody
    opened a checkout would let a shop collect real money by abandoning one.

    Collected by the **platform**: this is our income. Sending it through the
    owner's own keys would have the shop pay itself and leave us to invoice them
    for money they already hold.
    """
    plan = plan_by_public_id(db, body.plan_id)
    if plan is None:
        raise NotFound(NO_SUCH_PLAN)

    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        # Fail closed rather than falling back to the owner's own account,
        # which would collect our income into their till.
        raise BadRequest(NO_PLATFORM_KEYS)

    subscription = start_purchase(
        db,
        user_id=owner.id,
        plan=plan,
        duration_months=body.duration_months,
    )

    payment = open_checkout(
        db,
        razorpay,
        user_id=owner.id,
        kind=PaymentKind.SUBSCRIPTION,
        amount=subscription.total_amount,
        receipt=f"subscription:{subscription.public_id}",
        kiosk=None,
        credentials=Credentials(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
        gateway=Gateway.PLATFORM_GATEWAY,
        collecting_user_id=None,
        subscription_id=subscription.id,
    )

    return CheckoutResponse(
        razorpay_order_id=payment.razorpay_order_id,
        razorpay_key_id=settings.RAZORPAY_KEY_ID,
        amount_inr=payment.amount_inr,
        order_id=subscription.public_id,
    )


@router.post("/subscription/{subscription_id}/verify", response_model=MySubscriptionResponse)
def verify_subscription_payment(
    subscription_id: str,
    body: VerifyPaymentRequest,
    owner: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> MySubscriptionResponse:
    """The browser coming back from Razorpay, checked against the platform key.

    The webhook settles this too, and either may arrive first. Both were always
    meant to -- but until this route existed the webhook was the only path, so
    an owner who had just paid sat on a page that said "not active" for as long
    as the delivery took, or for ever if their endpoint was misconfigured. A
    purchase that can only complete out of sight is a button somebody presses
    twice.

    A callback that does not verify changes nothing at all: no capture, no term,
    no open payment gate. That matters more here than for a print, because a
    subscription in force is half of what lets a shop collect real money -- a
    forged receipt would be a shop turning its own takings on.

    The signature is checked against the **platform's** key secret, because a
    subscription is always collected by the platform. Reading the owner's keys
    here would let a shop sign its own subscription into force.
    """
    subscription = subscription_by_public_id(db, subscription_id)
    # Somebody else's is the same answer as one that never existed. A 403 would
    # tell one owner something true about another.
    if subscription is None or subscription.user_id != owner.id:
        raise NotFound(NO_SUCH_SUBSCRIPTION)

    payment = payment_for_razorpay_order(db, body.razorpay_order_id)
    # The subscription is named on the payment, so a genuine receipt for a
    # different purchase cannot start this one's term.
    if payment is None or payment.subscription_id != subscription.id:
        raise NotFound(NO_CHECKOUT)

    # Same race as the student's order: this route exists precisely because the
    # webhook may be slow or misconfigured, so the browser settling second is
    # the ordinary case rather than the exception. A 409 here told an owner who
    # had just paid that their payment could not be confirmed, on a page that
    # already said their subscription was not active.
    if (
        payment.status is PaymentStatus.CAPTURED
        and payment.razorpay_payment_id == body.razorpay_payment_id
    ):
        activate_subscription(db, subscription)
        return _as_subscription(subscription)

    confirm_payment(
        db,
        payment,
        razorpay_payment_id=body.razorpay_payment_id,
        signature=body.razorpay_signature,
        key_secret=settings.RAZORPAY_KEY_SECRET,
    )

    # Idempotent: an already-active subscription is returned untouched rather
    # than having its term extended a second time, which is what makes it safe
    # for this and the webhook to race.
    activate_subscription(db, subscription)
    return _as_subscription(subscription)


@router.get("/subscription/{subscription_id}/invoice")
def subscription_invoice(
    subscription_id: str,
    owner: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> Response:
    """The invoice for a subscription that has been paid for, as a PDF.

    **Bytes from an authenticated route, never a URL.** The same rule as the
    student receipt and the account-ownership proof: a document served from a
    guessable path is a document anybody can read, and one that 404s behind an
    image tag looks exactly like one that was never uploaded.

    Only the caller's own. An invoice carries a name, an address and what
    somebody pays for their software, so another owner's is a 404 -- the same
    answer as a subscription that never existed.

    The issuer comes from configuration rather than from a constant: whose name
    is at the top is a legal detail that changes without the software changing,
    and an invoice is the one document here a third party may hold us to.
    """
    subscription = subscription_by_public_id(db, subscription_id)
    if subscription is None or subscription.user_id != owner.id:
        raise NotFound(NO_SUCH_SUBSCRIPTION)

    payment = payment_for_subscription(db, subscription.id)

    pdf = render_subscription_invoice(
        subscription,
        plan_name=plan_named(db, subscription.plan_id),
        billed_to=InvoiceParty(
            name=owner.full_name or owner.email,
            email=owner.email,
            # Whatever else is on file. Today an account holds a name and an
            # address is not among the things it holds -- see the note in
            # `billing/invoice.py`. The renderer prints what it is handed, so
            # the day one is stored this is the only line that changes.
            lines=(),
        ),
        billed_by=InvoiceParty(
            name=settings.INVOICE_ISSUER_NAME,
            email=settings.INVOICE_ISSUER_EMAIL,
            lines=tuple(
                line.strip()
                for line in settings.INVOICE_ISSUER_LINES.split("|")
                if line.strip()
            ),
        ),
        paid_at=payment.captured_at if payment else None,
        payment_reference=payment.razorpay_payment_id if payment else None,
    )

    number = invoice_number(subscription)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            # Named by the invoice number printed on it, so a downloads folder
            # of these can be matched to a ledger without opening each one.
            # The number is our own id and carries nothing somebody else typed,
            # so it cannot rewrite the response headers.
            "Content-Disposition": f'attachment; filename="{number}.pdf"'
        },
    )
