"""Opening a checkout, and recording what came back.

The rule under test throughout: the account that collects and the key that
signs are decided together, once, and written down. Two services disagreeing
about that is what leaked money in the backend being replaced.
"""

import hashlib
import hmac
from decimal import Decimal

import pytest

from app.core.crypto import SecretBox
from app.core.errors import BadRequest, Conflict
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskAssignment
from app.modules.payments.charges import (
    Credentials,
    confirm_payment,
    credentials_for,
    open_checkout,
    payment_for_razorpay_order,
    record_capture,
    record_failure,
)
from app.modules.payments.gate import Gateway
from app.modules.payments.models import (
    Payment,
    PaymentKind,
    PaymentSource,
    PaymentStatus,
)

PLATFORM_KEY = "rzp_test_platform"
PLATFORM_SECRET = "platform_secret_value"


class FakeRazorpay:
    """Records what it was asked to open, and with which key."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.counter = 0

    def create_order(self, *, amount_paise, receipt, credentials) -> str:
        self.counter += 1
        self.calls.append(
            {
                "amount_paise": amount_paise,
                "receipt": receipt,
                "key_id": credentials.key_id,
                "key_secret": credentials.key_secret,
            }
        )
        return f"order_FAKE{self.counter}"


@pytest.fixture
def razorpay() -> FakeRazorpay:
    return FakeRazorpay()


@pytest.fixture
def box() -> SecretBox:
    return SecretBox("k" * 43 + "=")


@pytest.fixture
def student(db_session):
    user = User(email="payer@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def platform_kiosk(db_session):
    kiosk = Kiosk(
        name="Platform Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
    )
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


def sign(order_id: str, payment_id: str, secret: str) -> str:
    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def a_payment(db_session, razorpay, student, kiosk, amount="20.00") -> Payment:
    return open_checkout(
        db_session,
        razorpay,
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal(amount),
        receipt="ord_test",
        kiosk=kiosk,
        credentials=Credentials(PLATFORM_KEY, PLATFORM_SECRET),
        gateway=Gateway.PLATFORM_GATEWAY,
    )


# ── whose keys ──────────────────────────────────────────────────────────────


def test_a_platform_kiosk_uses_the_platform_keys(db_session, box, platform_kiosk):
    collection = credentials_for(
        db_session,
        platform_kiosk,
        box=box,
        platform_key_id=PLATFORM_KEY,
        platform_key_secret=PLATFORM_SECRET,
    )

    assert collection.gateway is Gateway.PLATFORM_GATEWAY
    assert collection.credentials.key_id == PLATFORM_KEY
    assert collection.collecting_user_id is None, "the platform collects, not an owner"


def test_a_closed_kiosk_cannot_open_a_checkout(db_session, box, platform_kiosk):
    """A SOLD kiosk with no owner, no subscription and no keys. The gate says
    CLOSED and there is no fallback to the platform's account -- that fallback
    is exactly how a shop's takings silently became the platform's."""
    platform_kiosk.kiosk_type = KioskType.SOLD
    db_session.flush()

    with pytest.raises(BadRequest):
        credentials_for(
            db_session,
            platform_kiosk,
            box=box,
            platform_key_id=PLATFORM_KEY,
            platform_key_secret=PLATFORM_SECRET,
        )


def test_a_platform_kiosk_with_no_configured_keys_fails_closed(
    db_session, box, platform_kiosk
):
    """Unconfigured platform keys refuse the payment. Falling back to any other
    key would collect into an account that has nothing to do with this kiosk."""
    with pytest.raises(BadRequest):
        credentials_for(
            db_session,
            platform_kiosk,
            box=box,
            platform_key_id="",
            platform_key_secret="",
        )


def test_an_owner_gateway_kiosk_uses_the_owners_decrypted_secret(
    db_session, box, platform_kiosk
):
    """The stored secret is ciphertext; what reaches Razorpay is the plaintext,
    and it never travels through an API response to get there."""
    from datetime import UTC, datetime, timedelta

    from app.modules.billing.models import Plan, Subscription, SubscriptionStatus
    from app.modules.identity import repository as identity_repo
    from app.modules.kiosks.enums import AssignmentRole
    from app.modules.payments.configs import set_keys

    owner = User(email="owner@example.com", hashed_password="x")
    db_session.add(owner)
    db_session.flush()
    identity_repo.grant_role(db_session, owner.id, Role.OWNER)

    platform_kiosk.kiosk_type = KioskType.SOLD
    db_session.add(
        KioskAssignment(
            kiosk_id=platform_kiosk.id, user_id=owner.id, role=AssignmentRole.OWNER
        )
    )
    plan = Plan(name="Charges Test Plan", monthly_price=Decimal("499.00"))
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        Subscription(
            user_id=owner.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            duration_months=1,
            monthly_price_charged=Decimal("499.00"),
            total_amount=Decimal("499.00"),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    set_keys(
        db_session,
        user_id=owner.id,
        key_id="rzp_live_owner",
        key_secret="the_owners_secret",
        box=box,
    )
    db_session.flush()

    collection = credentials_for(
        db_session,
        platform_kiosk,
        box=box,
        platform_key_id=PLATFORM_KEY,
        platform_key_secret=PLATFORM_SECRET,
    )

    assert collection.gateway is Gateway.OWNER_GATEWAY
    assert collection.credentials.key_id == "rzp_live_owner"
    assert collection.credentials.key_secret == "the_owners_secret"
    # Recorded so a webhook delivered to this owner's URL, and a refund months
    # later, both go to the account that actually collected.
    assert collection.collecting_user_id == owner.id


def test_a_lapsed_subscription_stops_collection_even_with_working_keys(
    db_session, box, platform_kiosk
):
    """D7, at the point where money is actually taken.

    The dangerous case is not the kiosk with nothing configured -- that one
    fails for lots of reasons. It is the kiosk whose owner still has perfectly
    good Razorpay keys and has simply stopped paying: without the explicit
    CLOSED check the code walks straight past the gate, finds those keys, and
    collects. A mutation that deleted the check passed every other test here.
    """
    from datetime import UTC, datetime, timedelta

    from app.modules.billing.models import Plan, Subscription, SubscriptionStatus
    from app.modules.identity import repository as identity_repo
    from app.modules.kiosks.enums import AssignmentRole
    from app.modules.payments.configs import set_keys

    owner = User(email="lapsed@example.com", hashed_password="x")
    db_session.add(owner)
    db_session.flush()
    identity_repo.grant_role(db_session, owner.id, Role.OWNER)

    platform_kiosk.kiosk_type = KioskType.SOLD
    db_session.add(
        KioskAssignment(
            kiosk_id=platform_kiosk.id, user_id=owner.id, role=AssignmentRole.OWNER
        )
    )
    plan = Plan(name="Lapsed Plan", monthly_price=Decimal("499.00"))
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        Subscription(
            user_id=owner.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            duration_months=1,
            monthly_price_charged=Decimal("499.00"),
            total_amount=Decimal("499.00"),
            # Paid up until last month, and not since.
            expires_at=datetime.now(UTC) - timedelta(days=30),
        )
    )
    set_keys(
        db_session,
        user_id=owner.id,
        key_id="rzp_live_still_valid",
        key_secret="still_works_fine",
        box=box,
    )
    db_session.flush()

    with pytest.raises(BadRequest):
        credentials_for(
            db_session,
            platform_kiosk,
            box=box,
            platform_key_id=PLATFORM_KEY,
            platform_key_secret=PLATFORM_SECRET,
        )


def test_credentials_never_print_their_secret():
    """A repr lands in logs and tracebacks."""
    assert "the_secret" not in repr(Credentials("rzp_x", "the_secret"))


# ── opening a checkout ──────────────────────────────────────────────────────


def test_opening_a_checkout_records_our_side_before_the_student_pays(
    db_session, razorpay, student, platform_kiosk
):
    """The row exists first, so a capture arriving by webhook always has
    something to attach to. Creating it on the way back would leave an
    unattributable payment every time somebody closed the tab."""
    payment = a_payment(db_session, razorpay, student, platform_kiosk)

    assert payment.status is PaymentStatus.CREATED
    assert payment.razorpay_order_id == "order_FAKE1"
    assert payment.public_id.startswith("pay_")


def test_the_amount_reaches_razorpay_in_paise(
    db_session, razorpay, student, platform_kiosk
):
    """Rupees here, integer paise at the boundary. Sending 20.00 as an amount
    would charge twenty paise."""
    a_payment(db_session, razorpay, student, platform_kiosk, amount="20.50")

    assert razorpay.calls[0]["amount_paise"] == 2050


def test_the_key_used_is_the_one_the_gate_chose(
    db_session, razorpay, student, platform_kiosk
):
    a_payment(db_session, razorpay, student, platform_kiosk)

    assert razorpay.calls[0]["key_id"] == PLATFORM_KEY


def test_the_payment_records_which_arrangement_collected(
    db_session, razorpay, student, platform_kiosk
):
    """Written down, not re-derived. A refund six months later must go back to
    the account that actually collected, whatever the kiosk looks like then."""
    payment = a_payment(db_session, razorpay, student, platform_kiosk)

    # A typed value, not a bare string: `EnumText` is what stops this reading
    # back as a plain str after a round-trip and quietly passing `== Gateway.X`
    # while `.value` raises.
    assert payment.source is PaymentSource.PLATFORM_GATEWAY
    assert payment.kiosk_id == platform_kiosk.id


def test_a_checkout_for_nothing_is_refused(
    db_session, razorpay, student, platform_kiosk
):
    with pytest.raises(BadRequest):
        a_payment(db_session, razorpay, student, platform_kiosk, amount="0.00")


def test_a_payment_can_be_found_by_its_razorpay_order_id(
    db_session, razorpay, student, platform_kiosk
):
    """How a webhook finds our row."""
    payment = a_payment(db_session, razorpay, student, platform_kiosk)

    assert payment_for_razorpay_order(db_session, "order_FAKE1") is payment
    assert payment_for_razorpay_order(db_session, "order_NOPE") is None


# ── confirming ──────────────────────────────────────────────────────────────


def test_a_genuine_callback_captures_the_payment(
    db_session, razorpay, student, platform_kiosk
):
    payment = a_payment(db_session, razorpay, student, platform_kiosk)

    confirm_payment(
        db_session,
        payment,
        razorpay_payment_id="pay_REAL",
        signature=sign("order_FAKE1", "pay_REAL", PLATFORM_SECRET),
        key_secret=PLATFORM_SECRET,
    )

    assert payment.status is PaymentStatus.CAPTURED
    assert payment.razorpay_payment_id == "pay_REAL"
    assert payment.captured_at is not None


def test_a_forged_callback_changes_nothing(
    db_session, razorpay, student, platform_kiosk
):
    """Not "changes nothing important" -- nothing. A forged callback that left
    a payment id behind would be a payment somebody could later reconcile."""
    payment = a_payment(db_session, razorpay, student, platform_kiosk)

    with pytest.raises(BadRequest):
        confirm_payment(
            db_session,
            payment,
            razorpay_payment_id="pay_FORGED",
            signature="deadbeef" * 8,
            key_secret=PLATFORM_SECRET,
        )

    assert payment.status is PaymentStatus.CREATED
    assert payment.razorpay_payment_id is None


def test_a_callback_signed_with_the_wrong_key_is_refused(
    db_session, razorpay, student, platform_kiosk
):
    """This is what stops one owner's Razorpay account authorising a payment
    that another owner's kiosk opened."""
    payment = a_payment(db_session, razorpay, student, platform_kiosk)

    with pytest.raises(BadRequest):
        confirm_payment(
            db_session,
            payment,
            razorpay_payment_id="pay_REAL",
            signature=sign("order_FAKE1", "pay_REAL", "somebody_elses_secret"),
            key_secret=PLATFORM_SECRET,
        )

    assert payment.status is PaymentStatus.CREATED


def test_a_signature_from_another_order_is_refused(
    db_session, razorpay, student, platform_kiosk
):
    first = a_payment(db_session, razorpay, student, platform_kiosk)
    second = a_payment(db_session, razorpay, student, platform_kiosk)

    with pytest.raises(BadRequest):
        confirm_payment(
            db_session,
            second,
            razorpay_payment_id="pay_REAL",
            signature=sign(first.razorpay_order_id, "pay_REAL", PLATFORM_SECRET),
            key_secret=PLATFORM_SECRET,
        )


def test_capturing_the_same_payment_twice_is_refused(
    db_session, razorpay, student, platform_kiosk
):
    """The webhook and the browser callback both arrive. Only one may count."""
    payment = a_payment(db_session, razorpay, student, platform_kiosk)
    record_capture(db_session, payment, razorpay_payment_id="pay_REAL")

    with pytest.raises(Conflict):
        record_capture(db_session, payment, razorpay_payment_id="pay_REAL")


def test_one_real_payment_cannot_be_claimed_by_two_of_our_rows(
    db_session, razorpay, student, platform_kiosk
):
    """The nullable-unique razorpay_payment_id, doing the job the spec gave it."""
    first = a_payment(db_session, razorpay, student, platform_kiosk)
    second = a_payment(db_session, razorpay, student, platform_kiosk)
    record_capture(db_session, first, razorpay_payment_id="pay_REAL")

    with pytest.raises(Conflict):
        record_capture(db_session, second, razorpay_payment_id="pay_REAL")


def test_a_failure_is_recorded_rather_than_forgotten(
    db_session, razorpay, student, platform_kiosk
):
    payment = a_payment(db_session, razorpay, student, platform_kiosk)

    record_failure(db_session, payment, reason="card declined")

    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_reason == "card declined"


def test_money_that_arrived_cannot_be_failed_afterwards(
    db_session, razorpay, student, platform_kiosk
):
    """Razorpay events can arrive out of order. A late `payment.failed` must not
    undo a capture that really happened."""
    payment = a_payment(db_session, razorpay, student, platform_kiosk)
    record_capture(db_session, payment, razorpay_payment_id="pay_REAL")

    with pytest.raises(Conflict):
        record_failure(db_session, payment, reason="late failure event")

    assert payment.status is PaymentStatus.CAPTURED
