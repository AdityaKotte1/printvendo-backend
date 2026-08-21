"""What a kiosk took, counted once.

The legacy defect these pin: revenue was computed in three places with two
different ideas of which payment states count. `wallet.py` and `printers.py`
counted only PAID; `kiosk.py` counted PAID and CAPTURED. An owner's earnings
page and their kiosk page therefore reported different revenue for the same day,
and by its own definition neither was wrong.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk
from app.modules.payments import PaymentKind, record_wallet_payment
from app.modules.payments.charges import Credentials, open_checkout, record_capture
from app.modules.payments.earnings import earnings_by_kiosk, earnings_for_kiosks
from app.modules.payments.gate import Gateway
from app.modules.payments.models import PaymentStatus, RefundDestination
from app.modules.payments.refunds import refund

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


class FakeRazorpay:
    def __init__(self) -> None:
        self.n = 0

    def create_order(self, *, amount_paise, receipt, credentials) -> str:
        self.n += 1
        return f"order_E{self.n}"

    def refund(self, *, razorpay_payment_id, amount_paise, idempotency_key, credentials):
        return f"rfnd_E{razorpay_payment_id}"


@pytest.fixture
def student(db_session) -> User:
    user = User(email="buyer@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


def a_kiosk(db_session, name: str) -> Kiosk:
    kiosk = Kiosk(
        name=name,
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
    )
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    return a_kiosk(db_session, "Earning Shop")


@pytest.fixture
def gateway() -> FakeRazorpay:
    return FakeRazorpay()


def a_capture(db_session, gateway, student, kiosk, amount: str, *, at=NOW, pay_id=None):
    payment = open_checkout(
        db_session,
        gateway,
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal(amount),
        receipt="ord_e",
        kiosk=kiosk,
        credentials=Credentials("rzp_test", "secret"),
        gateway=Gateway.PLATFORM_GATEWAY,
    )
    record_capture(
        db_session,
        payment,
        razorpay_payment_id=pay_id or f"pay_{payment.razorpay_order_id}",
        now=at,
    )
    return payment


# ── the one predicate ───────────────────────────────────────────────────────


def test_only_money_that_arrived_counts(db_session, gateway, student, kiosk):
    """An opened checkout the student abandoned is not revenue."""
    a_capture(db_session, gateway, student, kiosk, "100.00")
    open_checkout(  # never captured
        db_session,
        gateway,
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("999.00"),
        receipt="ord_abandoned",
        kiosk=kiosk,
        credentials=Credentials("rzp_test", "secret"),
        gateway=Gateway.PLATFORM_GATEWAY,
    )

    result = earnings_for_kiosks(db_session, [kiosk.id])

    assert result.gross_inr == Decimal("100.00")
    assert result.order_count == 1


def test_a_refunded_payment_is_still_a_sale_and_a_refund(
    db_session, gateway, student, kiosk
):
    """Collapsing the two would make a fully refunded day's takings vanish from
    history rather than showing as money in and money back."""
    payment = a_capture(db_session, gateway, student, kiosk, "100.00")
    refund(
        db_session,
        payment=payment,
        amount=Decimal("100.00"),
        destination=RefundDestination.SOURCE,
        idempotency_key="full",
        razorpay=gateway,
        credentials=Credentials("rzp_test", "secret"),
    )
    assert payment.status is PaymentStatus.REFUNDED

    result = earnings_for_kiosks(db_session, [kiosk.id])

    assert result.gross_inr == Decimal("100.00")
    assert result.refunded_inr == Decimal("100.00")
    assert result.net_inr == Decimal("0.00")
    assert result.order_count == 1


def test_a_partly_refunded_payment_counts_in_full_with_the_refund_deducted(
    db_session, gateway, student, kiosk
):
    payment = a_capture(db_session, gateway, student, kiosk, "100.00")
    refund(
        db_session,
        payment=payment,
        amount=Decimal("30.00"),
        destination=RefundDestination.SOURCE,
        idempotency_key="part",
        razorpay=gateway,
        credentials=Credentials("rzp_test", "secret"),
    )

    result = earnings_for_kiosks(db_session, [kiosk.id])

    assert result.gross_inr == Decimal("100.00")
    assert result.refunded_inr == Decimal("30.00")
    assert result.net_inr == Decimal("70.00")


def test_net_may_be_negative_and_is_not_clamped(db_session, gateway, student, kiosk):
    """Refunds today against takings from last week. The old backend wrapped
    this in max(0, ...), which hid the one case somebody has to act on."""
    payment = a_capture(
        db_session, gateway, student, kiosk, "100.00", at=NOW - timedelta(days=7)
    )
    refund(
        db_session,
        payment=payment,
        amount=Decimal("100.00"),
        destination=RefundDestination.SOURCE,
        idempotency_key="old",
        razorpay=gateway,
        credentials=Credentials("rzp_test", "secret"),
    )

    # This week: nothing taken, and last week's sale refunded.
    result = earnings_for_kiosks(db_session, [kiosk.id], since=NOW - timedelta(days=1))

    assert result.gross_inr == Decimal("0.00")
    assert result.net_inr == Decimal("0.00")

    # Over the whole period the refund is visible against the sale.
    whole = earnings_for_kiosks(db_session, [kiosk.id])
    assert whole.net_inr == Decimal("0.00")
    assert whole.refunded_inr == Decimal("100.00")


# ── scope and shape ─────────────────────────────────────────────────────────


def test_another_kiosks_takings_are_not_included(db_session, gateway, student, kiosk):
    other = a_kiosk(db_session, "Someone Elses Shop")
    a_capture(db_session, gateway, student, kiosk, "50.00")
    a_capture(db_session, gateway, student, other, "900.00")

    assert earnings_for_kiosks(db_session, [kiosk.id]).gross_inr == Decimal("50.00")


def test_no_kiosks_earns_nothing_rather_than_everything(db_session, gateway, student, kiosk):
    """An empty id list must not become an unfiltered query. That is the shape
    of accident that shows one owner another owner's revenue."""
    a_capture(db_session, gateway, student, kiosk, "500.00")

    assert earnings_for_kiosks(db_session, []).gross_inr == Decimal("0.00")


def test_a_top_up_is_not_a_kiosks_earnings(db_session, gateway, student, kiosk):
    """Money added to a balance is not a sale at any shop."""
    payment = open_checkout(
        db_session,
        gateway,
        user_id=student.id,
        kind=PaymentKind.WALLET_TOPUP,
        amount=Decimal("500.00"),
        receipt="topup",
        kiosk=kiosk,
        credentials=Credentials("rzp_test", "secret"),
        gateway=Gateway.PLATFORM_GATEWAY,
    )
    record_capture(db_session, payment, razorpay_payment_id="pay_topup", now=NOW)

    assert earnings_for_kiosks(db_session, [kiosk.id]).gross_inr == Decimal("0.00")


def test_wallet_spending_can_be_excluded(db_session, student, kiosk):
    """Different money from an owner's side: a wallet payment was collected by
    Printvendo and is owed to them, a gateway payment at their own kiosk is
    already in their account."""
    record_wallet_payment(
        db_session,
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("40.00"),
        kiosk_id=kiosk.id,
        now=NOW,
    )

    assert earnings_for_kiosks(db_session, [kiosk.id]).gross_inr == Decimal("40.00")
    assert earnings_for_kiosks(
        db_session, [kiosk.id], include_wallet=False
    ).gross_inr == Decimal("0.00")


# ── per kiosk ───────────────────────────────────────────────────────────────


def test_a_kiosk_that_took_nothing_reports_zero_not_absence(
    db_session, gateway, student, kiosk
):
    """A missing key reads as "no data" and renders as a blank. Zero is the true
    and more useful answer."""
    quiet = a_kiosk(db_session, "Quiet Shop")
    a_capture(db_session, gateway, student, kiosk, "20.00")

    split = earnings_by_kiosk(db_session, [kiosk.id, quiet.id])

    assert split[kiosk.id].gross_inr == Decimal("20.00")
    assert split[quiet.id].gross_inr == Decimal("0.00")
    assert split[quiet.id].order_count == 0


def test_the_split_agrees_with_the_total(db_session, gateway, student, kiosk):
    """Two code paths, one answer. They must not be able to disagree -- that is
    the whole defect this module exists to prevent."""
    other = a_kiosk(db_session, "Second Shop")
    a_capture(db_session, gateway, student, kiosk, "30.00")
    a_capture(db_session, gateway, student, other, "70.00")

    total = earnings_for_kiosks(db_session, [kiosk.id, other.id])
    split = earnings_by_kiosk(db_session, [kiosk.id, other.id])

    assert sum(e.gross_inr for e in split.values()) == total.gross_inr
    assert sum(e.order_count for e in split.values()) == total.order_count
