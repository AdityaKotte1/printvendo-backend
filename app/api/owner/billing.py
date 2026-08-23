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

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    get_db,
    get_razorpay,
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
)
from app.core.config import Settings
from app.core.errors import BadRequest, NotFound
from app.modules.billing import (
    Subscription,
    active_plans,
    active_subscription,
    effective_end,
    is_on_trial,
    plan_by_public_id,
    quote_subscription,
    start_purchase,
)
from app.modules.identity.roles import Role
from app.modules.payments import (
    Credentials,
    Gateway,
    PaymentKind,
    RazorpayGateway,
    open_checkout,
)

router = APIRouter(
    prefix="/v1/owner/billing",
    tags=["owner"],
    dependencies=[Depends(require_any_role(Role.OWNER, Role.ADMIN))],
)

NO_PLATFORM_KEYS = "Subscriptions cannot be bought right now. Please try again later."
NO_SUCH_PLAN = "That plan does not exist."


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
