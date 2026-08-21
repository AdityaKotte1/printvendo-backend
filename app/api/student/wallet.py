"""A student's balance, and putting money into it.

There is no `hold` here, and that absence is the point. The old backend's
`POST /wallet/hold` took money and left a *second* request to enqueue the print;
everything between the two was a state where a student had paid for nothing.
Spending happens inside `POST /orders/{id}/pay/wallet`, in one transaction with
the print tasks, so there is nothing left to hold.

Topping up opens a Razorpay order against the **platform's** account, always.
Balance is a liability Printvendo owes the student, so it cannot be funded into
a shop owner's account -- which is also why `wallet_may_be_spent` refuses to let
that balance be spent where an owner collects.
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    get_db,
    get_razorpay,
    get_settings_from_app,
)
from app.api.schemas import (
    CheckoutResponse,
    TopUpRequest,
    WalletEntryResponse,
    WalletResponse,
)
from app.core.config import Settings
from app.core.errors import BadRequest
from app.core.money import as_money
from app.modules.payments import (
    Credentials,
    Gateway,
    PaymentKind,
    RazorpayGateway,
    open_checkout,
)
from app.modules.wallet import balance_of, statement

router = APIRouter(prefix="/v1/app/wallet", tags=["student"])

NO_PLATFORM_KEYS = "Top-ups are unavailable right now. Please try again later."
MIN_TOPUP = Decimal("10.00")
MAX_TOPUP = Decimal("10000.00")


@router.get("", response_model=WalletResponse)
def my_balance(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> WalletResponse:
    """What this student can spend.

    Read from the wallet row, which the conditional UPDATE keeps identical to
    the sum of the ledger. A dispute is settled from the ledger, not from here.
    """
    return WalletResponse(balance_inr=balance_of(db, user_id=user.id))


@router.get("/statement", response_model=list[WalletEntryResponse])
def my_statement(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    limit: int = 100,
) -> list[WalletEntryResponse]:
    """What happened, newest first.

    Every row is a movement that actually occurred. The old ledger carried a
    `status`, so a row might not have happened and each reader had its own
    opinion about which statuses counted -- which is how a balance and its own
    ledger drifted apart.
    """
    return [
        WalletEntryResponse(
            id=entry.public_id,
            kind=entry.kind.value,
            amount_inr=entry.amount_inr,
            balance_after_inr=entry.balance_after_inr,
            note=entry.note,
            created_at=entry.created_at,
        )
        for entry in statement(db, user_id=user.id, limit=limit)
    ]


@router.post("/topup", response_model=CheckoutResponse)
def open_topup(
    body: TopUpRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
    razorpay: Annotated[RazorpayGateway, Depends(get_razorpay)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> CheckoutResponse:
    """Open a Razorpay order for a top-up.

    Nothing is credited here. The balance moves when the capture arrives -- by
    webhook or by the browser's callback -- keyed on `razorpay_payment_id`,
    which is unique per wallet, so a delivery repeated three times credits once.
    Crediting on this request instead would hand out balance for a checkout the
    student could simply abandon.

    `collecting_user_id` is left None: a top-up is always the platform's to
    collect, whatever kiosk the student happens to be standing at.
    """
    amount = as_money(body.amount_inr)
    if amount < MIN_TOPUP or amount > MAX_TOPUP:
        raise BadRequest(
            f"Top-ups must be between ₹{MIN_TOPUP:.0f} and ₹{MAX_TOPUP:.0f}."
        )

    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        # Fail closed. There is no owner account to fall back to here, and
        # falling back to one would fund a platform liability into a shop's till.
        raise BadRequest(NO_PLATFORM_KEYS)

    payment = open_checkout(
        db,
        razorpay,
        user_id=user.id,
        kind=PaymentKind.WALLET_TOPUP,
        amount=amount,
        receipt=f"topup:{user.public_id}",
        kiosk=None,
        credentials=Credentials(
            settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET
        ),
        gateway=Gateway.PLATFORM_GATEWAY,
        collecting_user_id=None,
    )

    return CheckoutResponse(
        razorpay_order_id=payment.razorpay_order_id,
        razorpay_key_id=settings.RAZORPAY_KEY_ID,
        amount_inr=payment.amount_inr,
        order_id=payment.public_id,
    )
