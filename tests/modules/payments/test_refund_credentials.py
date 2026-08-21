"""Which keys a refund is issued with.

`create_order` takes credentials per call because the gate decides them per
kiosk. `refund` was declared without any, which is a hole rather than a
simplification: a refund on money an owner collected has to be issued against
**that owner's** Razorpay account, and an account can only refund a payment it
took. Issuing it with the platform's keys does not move the money to the wrong
place -- Razorpay refuses outright, because the payment id is not theirs -- but
it turns a refund into a support ticket, and the caller has no way to tell.

The account is read off the Payment row, never re-derived: `collecting_user_id`
is the gate's answer recorded at checkout, and the owner's keys may have been
replaced since.
"""

from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import SecretBox
from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk
from app.modules.payments.charges import (
    Credentials,
    credentials_for_payment,
    open_checkout,
    record_capture,
)
from app.modules.payments.configs import set_keys
from app.modules.payments.gate import Gateway
from app.modules.payments.models import PaymentKind

BOX = SecretBox(Fernet.generate_key().decode())
PLATFORM = Credentials("rzp_platform", "platform_secret")


class FakeRazorpay:
    def __init__(self) -> None:
        self.counter = 0

    def create_order(self, *, amount_paise, receipt, credentials) -> str:
        self.counter += 1
        return f"order_FAKE{self.counter}"


@pytest.fixture
def student(db_session):
    user = User(email="refundcreds@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def owner(db_session):
    user = User(email="ownercreds@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    set_keys(
        db_session,
        user.id,
        key_id="rzp_owner_live",
        key_secret="owner_secret_value",
        box=BOX,
    )
    return user


@pytest.fixture
def kiosk(db_session):
    kiosk = Kiosk(
        name="Creds Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
    )
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


def a_payment(db_session, student, kiosk, *, collecting_user_id, gateway):
    payment = open_checkout(
        db_session,
        FakeRazorpay(),
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("50.00"),
        receipt="ord_creds",
        kiosk=kiosk,
        credentials=PLATFORM,
        gateway=gateway,
        collecting_user_id=collecting_user_id,
    )
    record_capture(
        db_session, payment, razorpay_payment_id=f"pay_{collecting_user_id or 'PLAT'}"
    )
    return payment


def test_platform_collected_money_refunds_with_the_platform_keys(
    db_session, student, kiosk
):
    payment = a_payment(
        db_session,
        student,
        kiosk,
        collecting_user_id=None,
        gateway=Gateway.PLATFORM_GATEWAY,
    )

    credentials = credentials_for_payment(
        db_session,
        payment,
        box=BOX,
        platform_key_id=PLATFORM.key_id,
        platform_key_secret=PLATFORM.key_secret,
    )

    assert credentials == PLATFORM


def test_owner_collected_money_refunds_with_that_owners_keys(
    db_session, student, kiosk, owner
):
    """The whole point. Refunding an owner's payment with the platform's keys is
    a request Razorpay refuses -- the payment id is not theirs."""
    payment = a_payment(
        db_session,
        student,
        kiosk,
        collecting_user_id=owner.id,
        gateway=Gateway.OWNER_GATEWAY,
    )

    credentials = credentials_for_payment(
        db_session,
        payment,
        box=BOX,
        platform_key_id=PLATFORM.key_id,
        platform_key_secret=PLATFORM.key_secret,
    )

    assert credentials.key_id == "rzp_owner_live"
    assert credentials.key_secret == "owner_secret_value"


def test_an_owner_whose_keys_are_gone_cannot_be_refunded_from(
    db_session, student, kiosk, owner
):
    """Falling back to the platform's keys here would be the worst option: it
    turns "we cannot reach this owner's account" into a confusing Razorpay
    rejection instead of a sentence a human can act on."""
    from app.core.errors import Conflict
    from app.modules.payments.models import KioskPaymentConfig

    payment = a_payment(
        db_session,
        student,
        kiosk,
        collecting_user_id=owner.id,
        gateway=Gateway.OWNER_GATEWAY,
    )

    config = (
        db_session.query(KioskPaymentConfig).filter_by(user_id=owner.id).one()
    )
    config.razorpay_key_id = None
    config.razorpay_key_secret_encrypted = None
    db_session.flush()

    with pytest.raises(Conflict):
        credentials_for_payment(
            db_session,
            payment,
            box=BOX,
            platform_key_id=PLATFORM.key_id,
            platform_key_secret=PLATFORM.key_secret,
        )


def test_a_wallet_payment_has_no_gateway_to_refund_through(
    db_session, student, kiosk
):
    """A balance-paid order never touched Razorpay, so asking which keys would
    refund it is a question with no answer -- and a to-source refund on it is
    already refused for the same reason."""
    from app.core.errors import Conflict
    from app.modules.payments import record_wallet_payment

    payment = record_wallet_payment(
        db_session,
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("50.00"),
        kiosk_id=kiosk.id,
    )

    with pytest.raises(Conflict):
        credentials_for_payment(
            db_session,
            payment,
            box=BOX,
            platform_key_id=PLATFORM.key_id,
            platform_key_secret=PLATFORM.key_secret,
        )


# ── and the refund actually uses them ───────────────────────────────────────


class RecordingGateway:
    """Records which keys it was handed."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_order(self, *, amount_paise, receipt, credentials) -> str:
        return "order_UNUSED"

    def refund(self, *, razorpay_payment_id, amount_paise, idempotency_key, credentials):
        self.calls.append({"payment_id": razorpay_payment_id, "credentials": credentials})
        return f"rfnd_{len(self.calls)}"


def test_a_refund_is_issued_with_the_collecting_accounts_keys(
    db_session, student, kiosk, owner
):
    from app.modules.payments import RefundDestination, refund

    payment = a_payment(
        db_session,
        student,
        kiosk,
        collecting_user_id=owner.id,
        gateway=Gateway.OWNER_GATEWAY,
    )
    credentials = credentials_for_payment(
        db_session,
        payment,
        box=BOX,
        platform_key_id=PLATFORM.key_id,
        platform_key_secret=PLATFORM.key_secret,
    )
    gateway = RecordingGateway()

    refund(
        db_session,
        payment=payment,
        amount=Decimal("10.00"),
        destination=RefundDestination.SOURCE,
        idempotency_key="creds_flow",
        razorpay=gateway,
        credentials=credentials,
    )

    assert gateway.calls[0]["credentials"].key_id == "rzp_owner_live"


def test_a_to_source_refund_without_credentials_is_refused(
    db_session, student, kiosk, owner
):
    """Fails closed. Calling Razorpay with no keys, or with whatever the gateway
    object happened to hold, is how a refund gets issued against the wrong
    account."""
    from app.core.errors import Conflict
    from app.modules.payments import RefundDestination, refund

    payment = a_payment(
        db_session,
        student,
        kiosk,
        collecting_user_id=owner.id,
        gateway=Gateway.OWNER_GATEWAY,
    )

    with pytest.raises(Conflict):
        refund(
            db_session,
            payment=payment,
            amount=Decimal("10.00"),
            destination=RefundDestination.SOURCE,
            idempotency_key="no_creds",
            razorpay=RecordingGateway(),
        )


def test_a_wallet_refund_needs_no_credentials_at_all(db_session, student, kiosk):
    """No gateway is involved, so demanding keys would refuse a refund that is
    entirely within our own books."""
    from app.modules.payments import RefundDestination, refund

    payment = a_payment(
        db_session,
        student,
        kiosk,
        collecting_user_id=None,
        gateway=Gateway.PLATFORM_GATEWAY,
    )

    row = refund(
        db_session,
        payment=payment,
        amount=Decimal("10.00"),
        destination=RefundDestination.WALLET,
        idempotency_key="wallet_no_creds",
    )

    assert row.razorpay_refund_id is None
