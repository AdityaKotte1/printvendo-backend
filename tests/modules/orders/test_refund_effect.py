"""What a refund means for the order it paid for.

The legacy audit's `REFUND_PENDING` was "written by one line, read by nothing,
accumulating real money owed". `OrderState.REFUNDED` was in exactly that
position until this existed: a value the enum could hold and nothing ever set.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskPaper
from app.modules.orders.models import OrderState, PaymentMethod
from app.modules.orders.refunds import apply_payment_refund
from app.modules.orders.service import RequestedDocument, pay_with_wallet, place_order
from app.modules.printing import PrintOptions
from app.modules.printing.models import Document, DocumentState
from app.modules.wallet.ledger import credit
from app.modules.wallet.models import EntryKind


@pytest.fixture
def user(db_session):
    user = User(email="refundeffect@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def kiosk(db_session):
    kiosk = Kiosk(
        name="Refund Effect Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
        accepts_wallet=True,
        price_bw_single=Decimal("2.00"),
        price_bw_double=Decimal("1.50"),
        price_color_single=Decimal("10.00"),
        price_color_double=Decimal("8.00"),
    )
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id, capacity=500, used=0))
    db_session.flush()
    return kiosk


@pytest.fixture
def paid_order(db_session, user, kiosk):
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("500.00"),
        kind=EntryKind.TOPUP,
        reference="pay_seed_refund_effect",
    )
    document = Document(
        user_id=user.id,
        original_filename="essay.pdf",
        page_count=10,
        original_path="originals/2026/08/re.pdf",
        state=DocumentState.READY,
    )
    db_session.add(document)
    db_session.flush()

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[
            RequestedDocument(
                document=document, options=PrintOptions.create(total_pages=10)
            )
        ],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)
    return order


def test_a_full_refund_closes_the_order(db_session, paid_order):
    apply_payment_refund(
        db_session,
        order_id=paid_order.id,
        refunded_inr=paid_order.total_inr,
        paid_inr=paid_order.total_inr,
    )

    assert paid_order.state is OrderState.REFUNDED


def test_a_partial_refund_leaves_the_order_where_it_was(db_session, paid_order):
    """Half the money back is not a closed order. The student still has print
    tasks queued, and calling it REFUNDED would tell the operator to stop."""
    before = paid_order.state

    apply_payment_refund(
        db_session,
        order_id=paid_order.id,
        refunded_inr=paid_order.total_inr / 2,
        paid_inr=paid_order.total_inr,
    )

    assert paid_order.state is before


def test_a_full_refund_after_printing_still_closes_the_order(db_session, paid_order):
    """A goodwill refund on a job that printed. REFUNDED is a statement about
    the money, and it is true here; what printed is on the PrintTask rows, which
    is where that question is actually answered."""
    paid_order.state = OrderState.COMPLETED
    db_session.flush()

    apply_payment_refund(
        db_session,
        order_id=paid_order.id,
        refunded_inr=paid_order.total_inr,
        paid_inr=paid_order.total_inr,
    )

    assert paid_order.state is OrderState.REFUNDED


def test_refunding_records_when_it_happened(db_session, paid_order):
    now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    apply_payment_refund(
        db_session,
        order_id=paid_order.id,
        refunded_inr=paid_order.total_inr,
        paid_inr=paid_order.total_inr,
        now=now,
    )

    assert paid_order.refunded_at == now


def test_a_refund_against_no_order_is_not_an_error(db_session):
    """A wallet top-up is a Payment with no order. Refunding one must not raise
    on the way past -- there is simply nothing here to close."""
    assert (
        apply_payment_refund(
            db_session,
            order_id=None,
            refunded_inr=Decimal("10.00"),
            paid_inr=Decimal("10.00"),
        )
        is None
    )


# ── the seam is actually connected ──────────────────────────────────────────


def test_the_refund_sink_at_the_composition_root_closes_the_order(
    db_session, paid_order
):
    """A protocol with no implementation is a seam nobody uses. This is the
    whole path: payments issues the refund, and the order closes -- without
    payments importing orders."""
    from app.api.deps import get_refund_sink
    from app.modules.payments import RefundDestination, refund
    from app.modules.payments.models import Payment

    payment = db_session.query(Payment).filter_by(order_id=paid_order.id).one()

    refund(
        db_session,
        payment=payment,
        amount=paid_order.total_inr,
        destination=RefundDestination.WALLET,
        idempotency_key=f"refund:{paid_order.public_id}",
        sink=get_refund_sink(),
    )

    assert paid_order.state is OrderState.REFUNDED
    assert paid_order.refunded_at is not None


def test_a_partial_refund_through_the_sink_leaves_the_order_open(
    db_session, paid_order
):
    from app.api.deps import get_refund_sink
    from app.modules.payments import RefundDestination, refund
    from app.modules.payments.models import Payment

    payment = db_session.query(Payment).filter_by(order_id=paid_order.id).one()

    refund(
        db_session,
        payment=payment,
        amount=Decimal("1.00"),
        destination=RefundDestination.WALLET,
        idempotency_key="partial_through_sink",
        sink=get_refund_sink(),
    )

    assert paid_order.state is not OrderState.REFUNDED
    assert paid_order.refunded_at is None
