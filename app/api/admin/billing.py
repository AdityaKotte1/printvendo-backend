"""Plans, negotiated terms, and the trial that turns a shop's takings on.

D13 as a surface. Three of these routes decide money:

* **A trial is not a courtesy.** A subscription inside its trial is in force,
  and being in force is half of what `kiosk_payment_gate` requires before a SOLD
  or SAAS kiosk collects into its owner's own Razorpay. Granting one lets that
  shop start taking student money; it lapsing suspends the kiosk again.
* **A per-owner rate overrides the published ladder.** The old system could
  change an owner's monthly price, or change a discount for everyone on a plan,
  but not give one owner a different annual rate -- so that happened in side
  agreements nothing enforced.
* **A negotiated price is recorded, not calculated here.** `quote_subscription`
  is the only thing that prices anything, and this router hands it the terms.

Every write is audited against the **owner**, beside their payment
configuration, because "what has this account been promised, and by whom" is one
question. A plan is audited as a plan: it is not somebody's account, and filing
it under the admin who created it would make its own history unanswerable.
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.api.schemas import (
    CreatePlanRequest,
    GrantTrialRequest,
    NegotiatePriceRequest,
    OwnerBillingResponse,
    OwnerDiscountResponse,
    PlanDiscountResponse,
    PlanResponse,
    QuoteResponse,
    SetDiscountRequest,
    SubscriptionResponse,
    UpdatePlanRequest,
)
from app.core.errors import NotFound
from app.modules.billing import (
    Plan,
    Subscription,
    SubscriptionStatus,
    active_plans,
    active_subscription,
    all_plans,
    clear_owner_discount,
    create_plan,
    end_trial,
    grant_trial,
    is_in_force,
    is_on_trial,
    owner_discounts,
    plan_by_public_id,
    plan_discounts,
    quote_subscription,
    set_negotiated_price,
    set_owner_discount,
    set_plan_discount,
    subscriptions_of,
    update_plan,
)
from app.modules.identity import User
from app.modules.identity import repository as identity_repo
from app.modules.identity.roles import Role
from app.modules.ops import audit

router = APIRouter(prefix="/v1/admin", tags=["admin"])

CurrentAdmin = Annotated[User, Depends(require_role(Role.ADMIN))]

NO_SUCH_OWNER = "That account does not exist."
NO_TRIAL = "That account is not on a trial."


def _plan_response(db: Session, plan: Plan) -> PlanResponse:
    return PlanResponse(
        id=plan.public_id,
        name=plan.name,
        monthly_price=plan.monthly_price,
        max_kiosks=plan.max_kiosks,
        price_floor_bw=plan.price_floor_bw,
        price_ceiling_bw=plan.price_ceiling_bw,
        price_floor_color=plan.price_floor_color,
        price_ceiling_color=plan.price_ceiling_color,
        is_active=plan.is_active,
        discounts=[
            PlanDiscountResponse(
                duration_months=discount.duration_months, percent=discount.percent
            )
            for discount in plan_discounts(db, plan)
        ],
    )


def _plan_terms(plan: Plan) -> dict:
    """What an audit entry says about a plan. Decimals become strings on the way
    in -- `ops.audit.scrub` does that, and money through a float is money that
    has lost precision in the one place people look."""
    return {
        "name": plan.name,
        "monthly_price": plan.monthly_price,
        "max_kiosks": plan.max_kiosks,
        "is_active": plan.is_active,
    }


def _subscription_response(
    db: Session, subscription: Subscription
) -> SubscriptionResponse:
    plan = db.get(Plan, subscription.plan_id)
    return SubscriptionResponse(
        id=subscription.public_id,
        plan_id=plan.public_id if plan else "",
        plan_name=plan.name if plan else "",
        status=SubscriptionStatus(subscription.status).value,
        on_trial=is_on_trial(subscription),
        in_force=is_in_force(subscription),
        monthly_price_charged=subscription.monthly_price_charged,
        negotiated_price=subscription.negotiated_price,
        total_amount=subscription.total_amount,
        free_until=subscription.free_until,
        starts_at=subscription.starts_at,
        expires_at=subscription.expires_at,
    )


def _owner(db: Session, owner_id: str) -> User:
    """The account these terms are about.

    404 for an id that is not a user id at all, the same as for one that is --
    `get_by_public_id` refuses the wrong kind rather than looking it up, so a
    kiosk id here cannot resolve to somebody's billing.
    """
    owner = identity_repo.get_by_public_id(db, owner_id)
    if owner is None:
        raise NotFound(NO_SUCH_OWNER)
    return owner


def _latest_subscription(db: Session, user_id: int) -> Subscription | None:
    """The one that matters: in force if any is, otherwise the most recent.

    An expired subscription is still worth showing -- "this shop's trial ran out
    in June" is the answer to why their kiosk stopped collecting.
    """
    return active_subscription(db, user_id) or next(
        iter(subscriptions_of(db, user_id)), None
    )


def _billing_response(db: Session, owner: User) -> OwnerBillingResponse:
    subscription = _latest_subscription(db, owner.id)
    return OwnerBillingResponse(
        owner_id=owner.public_id,
        owner_email=owner.email,
        subscription=(
            _subscription_response(db, subscription) if subscription else None
        ),
        discounts=[
            OwnerDiscountResponse(
                duration_months=discount.duration_months,
                percent=discount.percent,
                note=discount.note,
            )
            for discount in owner_discounts(db, owner.id)
        ],
    )


# ── plans ───────────────────────────────────────────────────────────────────


@router.get("/plans", response_model=list[PlanResponse])
def plans(
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    include_retired: bool = False,
) -> list[PlanResponse]:
    """What can be sold, cheapest first. Retired ones on request, because
    somebody is still on them."""
    listing = all_plans(db) if include_retired else active_plans(db)
    return [_plan_response(db, plan) for plan in listing]


@router.post("/plans", response_model=PlanResponse, status_code=status.HTTP_201_CREATED)
def add_plan(
    body: CreatePlanRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> PlanResponse:
    plan = create_plan(
        db,
        name=body.name,
        monthly_price=body.monthly_price,
        max_kiosks=body.max_kiosks,
        price_floor_bw=body.price_floor_bw,
        price_ceiling_bw=body.price_ceiling_bw,
        price_floor_color=body.price_floor_color,
        price_ceiling_color=body.price_ceiling_color,
    )

    audit.record(
        db,
        action="billing.plan.created",
        entity_type="plan",
        entity_id=plan.public_id,
        actor_user_id=admin.id,
        after=_plan_terms(plan),
    )
    return _plan_response(db, plan)


@router.patch("/plans/{plan_id}", response_model=PlanResponse)
def change_plan(
    plan_id: str,
    body: UpdatePlanRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> PlanResponse:
    """Change a plan's terms, or take it off sale.

    Changing the price does not change what anybody is already paying:
    `monthly_price_charged` is stored on the subscription at purchase precisely
    so a later price change cannot rewrite an existing charge.
    """
    plan = plan_by_public_id(db, plan_id)
    before = _plan_terms(plan)

    update_plan(
        db,
        plan,
        monthly_price=body.monthly_price,
        max_kiosks=body.max_kiosks,
        price_floor_bw=body.price_floor_bw,
        price_ceiling_bw=body.price_ceiling_bw,
        price_floor_color=body.price_floor_color,
        price_ceiling_color=body.price_ceiling_color,
        is_active=body.is_active,
    )

    audit.record(
        db,
        action="billing.plan.updated",
        entity_type="plan",
        entity_id=plan.public_id,
        actor_user_id=admin.id,
        before=before,
        after=_plan_terms(plan),
    )
    return _plan_response(db, plan)


@router.put("/plans/{plan_id}/discounts", response_model=PlanResponse)
def set_published_discount(
    plan_id: str,
    body: SetDiscountRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> PlanResponse:
    """The list price for paying several months up front."""
    plan = plan_by_public_id(db, plan_id)

    set_plan_discount(
        db, plan, duration_months=body.duration_months, percent=body.percent
    )

    audit.record(
        db,
        action="billing.plan.discount.set",
        entity_type="plan",
        entity_id=plan.public_id,
        actor_user_id=admin.id,
        after={"duration_months": body.duration_months, "percent": body.percent},
    )
    return _plan_response(db, plan)


# ── one owner's terms ───────────────────────────────────────────────────────


@router.get("/owners/{owner_id}/billing", response_model=OwnerBillingResponse)
def owner_billing(
    owner_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> OwnerBillingResponse:
    """What this account is on, and what it was promised."""
    return _billing_response(db, _owner(db, owner_id))


@router.get("/owners/{owner_id}/billing/quote", response_model=QuoteResponse)
def owner_quote(
    owner_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    duration_months: Annotated[int, Query(ge=1)] = 12,
) -> QuoteResponse:
    """What this owner would pay to renew, with every input that produced it.

    The same `quote_subscription` a purchase goes through, so a figure quoted
    here and a figure charged later cannot disagree.
    """
    owner = _owner(db, owner_id)
    subscription = _latest_subscription(db, owner.id)
    if subscription is None:
        raise NotFound("That account has no subscription to price.")

    plan = db.get(Plan, subscription.plan_id)
    if plan is None:
        raise NotFound("That subscription's plan no longer exists.")

    quote = quote_subscription(
        db,
        user_id=owner.id,
        plan=plan,
        duration_months=duration_months,
        negotiated_price=subscription.negotiated_price,
    )
    return QuoteResponse(
        duration_months=quote.duration_months,
        monthly_price=quote.monthly_price,
        discount_percent=quote.discount_percent,
        discount_source=quote.discount_source,
        negotiated=quote.negotiated,
        total=quote.total,
    )


@router.post("/owners/{owner_id}/billing/trial", response_model=OwnerBillingResponse)
def grant_owner_trial(
    owner_id: str,
    body: GrantTrialRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> OwnerBillingResponse:
    """Let this owner collect for a while without paying.

    Granting a second trial extends the first rather than adding a row -- two
    live trials would make "when does this stop being free" a question with two
    answers, and the shorter one could then never end anything.
    """
    owner = _owner(db, owner_id)
    plan = plan_by_public_id(db, body.plan_id)

    subscription = grant_trial(db, user_id=owner.id, plan=plan, days=body.days)

    audit.record(
        db,
        action="billing.trial.granted",
        entity_type="user",
        entity_id=owner.public_id,
        actor_user_id=admin.id,
        after={
            "plan_id": plan.public_id,
            "days": body.days,
            "free_until": subscription.free_until,
        },
    )
    return _billing_response(db, owner)


@router.delete("/owners/{owner_id}/billing/trial", response_model=OwnerBillingResponse)
def stop_owner_trial(
    owner_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> OwnerBillingResponse:
    """End a trial today rather than in three weeks."""
    owner = _owner(db, owner_id)

    subscription = active_subscription(db, owner.id)
    if subscription is None or not is_on_trial(subscription):
        raise NotFound(NO_TRIAL)

    end_trial(db, subscription)

    audit.record(
        db,
        action="billing.trial.ended",
        entity_type="user",
        entity_id=owner.public_id,
        actor_user_id=admin.id,
        after={"free_until": subscription.free_until},
    )
    return _billing_response(db, owner)


@router.put("/owners/{owner_id}/billing/price", response_model=OwnerBillingResponse)
def negotiate_price(
    owner_id: str,
    body: NegotiatePriceRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> OwnerBillingResponse:
    """What this owner pays a month, whatever the plan says. Null puts them back
    on the list price."""
    owner = _owner(db, owner_id)

    subscription = _latest_subscription(db, owner.id)
    if subscription is None:
        raise NotFound("That account has no subscription to reprice.")

    before: Decimal | None = subscription.negotiated_price
    set_negotiated_price(db, subscription, body.monthly_price)

    audit.record(
        db,
        action="billing.price.negotiated",
        entity_type="user",
        entity_id=owner.public_id,
        actor_user_id=admin.id,
        before={"negotiated_price": before},
        after={"negotiated_price": subscription.negotiated_price},
    )
    return _billing_response(db, owner)


@router.put("/owners/{owner_id}/billing/discounts", response_model=OwnerBillingResponse)
def set_owner_rate(
    owner_id: str,
    body: SetDiscountRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> OwnerBillingResponse:
    """A rate this owner gets and nobody else does (D13)."""
    owner = _owner(db, owner_id)

    set_owner_discount(
        db,
        user_id=owner.id,
        duration_months=body.duration_months,
        percent=body.percent,
        granted_by=admin.id,
        note=body.note,
    )

    audit.record(
        db,
        action="billing.discount.set",
        entity_type="user",
        entity_id=owner.public_id,
        actor_user_id=admin.id,
        after={"duration_months": body.duration_months, "percent": body.percent},
        note=body.note,
    )
    return _billing_response(db, owner)


@router.delete(
    "/owners/{owner_id}/billing/discounts/{duration_months}",
    response_model=OwnerBillingResponse,
)
def clear_owner_rate(
    owner_id: str,
    duration_months: int,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> OwnerBillingResponse:
    """Put this owner back on the published ladder."""
    owner = _owner(db, owner_id)

    clear_owner_discount(db, user_id=owner.id, duration_months=duration_months)

    audit.record(
        db,
        action="billing.discount.cleared",
        entity_type="user",
        entity_id=owner.public_id,
        actor_user_id=admin.id,
        after={"duration_months": duration_months},
    )
    return _billing_response(db, owner)
