"""Paying for an order out of a wallet balance.

The old backend's `POST /wallet/hold` debited and marked a payment PAID, then
left the caller to make a second request that actually enqueued the print. Every
test here exists to keep the debit and the tasks in one commit.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import BadRequest, Conflict
from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskPaper
from app.modules.orders.models import OrderState, PaymentMethod
from app.modules.orders.service import RequestedDocument, pay_with_wallet, place_order
from app.modules.printing import PrintOptions
from app.modules.printing.models import Document, DocumentState, PrintTask
from app.modules.wallet.ledger import balance_of, credit
from app.modules.wallet.models import EntryKind, WalletEntry


@pytest.fixture
def user(db_session):
    user = User(email="walletpay@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def kiosk(db_session):
    kiosk = Kiosk(
        name="Wallet Pay Shop",
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


def an_order(db_session, user, kiosk, *, pages: int = 10, method=PaymentMethod.WALLET):
    document = Document(
        user_id=user.id,
        original_filename="essay.pdf",
        page_count=pages,
        original_path="originals/2026/08/x.pdf",
        state=DocumentState.READY,
    )
    db_session.add(document)
    db_session.flush()

    return place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[
            RequestedDocument(
                document=document, options=PrintOptions.create(total_pages=pages)
            )
        ],
        method=method,
    )


def top_up(db_session, user, amount: str) -> None:
    credit(
        db_session,
        user_id=user.id,
        amount=Decimal(amount),
        kind=EntryKind.TOPUP,
        reference=f"pay_seed_{amount}",
    )


# ── the one commit ──────────────────────────────────────────────────────────


def test_paying_debits_the_wallet_and_queues_the_print(db_session, user, kiosk):
    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)

    tasks = pay_with_wallet(db_session, order)

    assert order.state is OrderState.PAID
    assert balance_of(db_session, user_id=user.id) == Decimal("80.00")
    assert len(tasks) == 1


def test_the_ledger_entry_names_the_order(db_session, user, kiosk):
    """So a student looking at "-₹20" can be told what it was for, and support
    can trace a disputed line back to a job."""
    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)

    pay_with_wallet(db_session, order)

    spend = (
        db_session.query(WalletEntry).filter_by(reference=order.public_id).one()
    )
    assert spend.amount_inr == Decimal("-20.00")


def test_an_empty_wallet_neither_pays_nor_prints(db_session, user, kiosk):
    """The failure has to take both halves with it, or a student is charged
    nothing and gets nothing, or worse, the reverse."""
    order = an_order(db_session, user, kiosk)

    with pytest.raises(BadRequest):
        pay_with_wallet(db_session, order)

    assert order.state is OrderState.AWAITING_PAYMENT
    assert db_session.query(PrintTask).count() == 0


def test_a_balance_one_paisa_short_pays_for_nothing(db_session, user, kiosk):
    top_up(db_session, user, "19.99")
    order = an_order(db_session, user, kiosk)

    with pytest.raises(BadRequest):
        pay_with_wallet(db_session, order)

    assert balance_of(db_session, user_id=user.id) == Decimal("19.99")
    assert db_session.query(PrintTask).count() == 0


def test_paying_twice_debits_once(db_session, user, kiosk):
    """A retried request. The order's own id is the wallet reference, and
    references are unique per wallet, so the ledger refuses the second."""
    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)
    pay_with_wallet(db_session, order)

    with pytest.raises(Conflict):
        pay_with_wallet(db_session, order)

    assert balance_of(db_session, user_id=user.id) == Decimal("80.00")
    assert db_session.query(PrintTask).count() == 1


def test_an_expired_order_is_refused_before_the_wallet_is_touched(
    db_session, user, kiosk
):
    """Checks run before the debit: refusing is free until money has moved."""
    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)
    order.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    with pytest.raises(Conflict):
        pay_with_wallet(db_session, order)

    assert balance_of(db_session, user_id=user.id) == Decimal("100.00")


def test_a_gateway_order_cannot_be_quietly_paid_from_the_wallet(
    db_session, user, kiosk
):
    """It was quoted with the gateway fee. Paying it from the wallet would take
    a fee the wallet does not charge, and the totals would not reconcile."""
    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk, method=PaymentMethod.GATEWAY)

    with pytest.raises(BadRequest):
        pay_with_wallet(db_session, order)

    assert balance_of(db_session, user_id=user.id) == Decimal("100.00")


def test_a_failure_while_queueing_takes_the_debit_with_it(
    db_session, user, kiosk, monkeypatch
):
    """The guarantee, stated as a test.

    Modelled with a savepoint, which is what the request transaction does in
    production: `get_db` rolls back on any exception, so the debit and the PAID
    flag go together or not at all.
    """
    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)

    def _explode(*args, **kwargs):
        raise RuntimeError("the queue is on fire")

    monkeypatch.setattr("app.modules.orders.service.enqueue_task", _explode)

    savepoint = db_session.begin_nested()
    with pytest.raises(RuntimeError):
        pay_with_wallet(db_session, order)
    savepoint.rollback()
    db_session.expire_all()

    assert order.state is OrderState.AWAITING_PAYMENT
    assert balance_of(db_session, user_id=user.id) == Decimal("100.00")
    assert db_session.query(PrintTask).count() == 0


# ── a wallet payment is a Payment ───────────────────────────────────────────


def test_paying_from_the_wallet_records_a_payment(db_session, user, kiosk):
    """Every paid order gets a Payment row, whatever paid for it.

    Without this a wallet-paid order has nothing to refund against: the refund
    path counts against `Payment.refunded_inr` and flips `Payment.status`, and
    an order that never wrote one simply could not be refunded. Two records of
    "this order was paid" would mean two implementations of "how much of it has
    been given back", and those are exactly the pair that drifts.
    """
    from app.modules.payments import PaymentKind, PaymentSource, PaymentStatus
    from app.modules.payments.models import Payment

    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)
    pay_with_wallet(db_session, order)

    payment = db_session.query(Payment).filter_by(order_id=order.id).one()

    assert payment.status is PaymentStatus.CAPTURED
    assert payment.source is PaymentSource.WALLET
    assert payment.kind is PaymentKind.PRINT_ORDER
    assert payment.amount_inr == order.total_inr
    assert payment.user_id == user.id
    assert payment.kiosk_id == kiosk.id
    assert payment.captured_at is not None


def test_a_wallet_payment_has_no_razorpay_identity(db_session, user, kiosk):
    """No order was opened and nothing was captured at a gateway, so there is
    no id to record. The absence is what later refuses a to-source refund."""
    from app.modules.payments.models import Payment

    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)
    pay_with_wallet(db_session, order)

    payment = db_session.query(Payment).filter_by(order_id=order.id).one()

    assert payment.razorpay_order_id is None
    assert payment.razorpay_payment_id is None
    assert payment.collecting_user_id is None


def test_a_refused_wallet_payment_records_no_payment(db_session, user, kiosk):
    """The debit and the Payment row are one commit. An empty wallet must leave
    neither -- a Payment row for money that never moved is worse than no row."""
    from app.modules.payments.models import Payment

    top_up(db_session, user, "1.00")
    order = an_order(db_session, user, kiosk)

    with pytest.raises(BadRequest):
        pay_with_wallet(db_session, order)

    assert db_session.query(Payment).filter_by(order_id=order.id).count() == 0


# ── and so it can be refunded ───────────────────────────────────────────────


def test_a_wallet_paid_order_is_refunded_by_the_one_refund_path(
    db_session, user, kiosk
):
    """The point of the whole exercise: one refund implementation, reached the
    same way whatever the student paid with."""
    from app.modules.payments import RefundDestination, refund
    from app.modules.payments.models import Payment

    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)
    pay_with_wallet(db_session, order)
    spent = order.total_inr

    payment = db_session.query(Payment).filter_by(order_id=order.id).one()
    refund(
        db_session,
        payment=payment,
        amount=spent,
        destination=RefundDestination.WALLET,
        idempotency_key=f"refund:{order.public_id}",
    )

    assert balance_of(db_session, user_id=user.id) == Decimal("100.00")
    assert payment.refunded_inr == spent


def test_wallet_money_cannot_be_refunded_to_a_source_that_does_not_exist(
    db_session, user, kiosk
):
    """There is no gateway payment to reverse. Refusing is not a formality:
    honouring it would call Razorpay with `razorpay_payment_id=None`."""
    from app.core.errors import BadRequest as Refused
    from app.modules.payments import RefundDestination, refund
    from app.modules.payments.models import Payment

    top_up(db_session, user, "100.00")
    order = an_order(db_session, user, kiosk)
    pay_with_wallet(db_session, order)

    payment = db_session.query(Payment).filter_by(order_id=order.id).one()

    with pytest.raises(Refused):
        refund(
            db_session,
            payment=payment,
            amount=Decimal("1.00"),
            destination=RefundDestination.SOURCE,
            idempotency_key="to_source",
            razorpay=object(),
        )

    assert payment.refunded_inr == Decimal("0.00")
