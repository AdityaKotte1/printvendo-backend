"""Money given back.

One rule, enforced once: the destination is forced by the account that collected.
The platform collected it, so it may go back to the student's wallet or to their
card. An owner collected it, so it may only go back to the student. Asking for
the wrong destination is refused rather than silently honoured -- honouring it
would move money out of an account the platform does not control.

A second rule, enforced by the database: the idempotency key. A refund that
times out and is retried finds the row already written instead of issuing it
again, whatever the destination.
"""

import itertools
import random
from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.core.errors import BadRequest, Conflict
from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk
from app.modules.payments.charges import Credentials, open_checkout, record_capture
from app.modules.payments.gate import Gateway
from app.modules.payments.models import Payment, PaymentKind, PaymentStatus, RefundDestination
from app.modules.payments.refunds import refund
from app.modules.wallet.models import EntryKind

PLATFORM_SECRET = "platform_secret_value"
SOME_KEYS = Credentials("rzp_test", PLATFORM_SECRET)


class FakeRazorpay:
    """Records the refund it was asked to issue, and refuses a second one."""

    #: Shared across instances so two fixtures opening the same amount do not
    #: collide on razorpay_order_id, which is unique.
    _orders = itertools.count(1)

    def __init__(self) -> None:
        self.refunds: list[dict] = []

    def create_order(self, *, amount_paise, receipt, credentials) -> str:
        return f"order_FAKE{next(self._orders)}"

    def refund(
        self, *, razorpay_payment_id, amount_paise, idempotency_key, credentials
    ) -> str:
        existing = [r for r in self.refunds if r["idempotency_key"] == idempotency_key]
        assert not existing, "the gateway was asked twice for the same refund"
        self.refunds.append(
            {
                "razorpay_payment_id": razorpay_payment_id,
                "amount_paise": amount_paise,
                "idempotency_key": idempotency_key,
                "credentials": credentials,
            }
        )
        return f"rfd_FAKE{len(self.refunds)}"


@dataclass
class SpySink:
    """What a refund means for the thing the payment was for."""

    refunds: list = None

    def __post_init__(self) -> None:
        self.refunds = []

    def on_refund(self, db, payment, refund_row) -> None:
        self.refunds.append(refund_row)


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


@pytest.fixture
def platform_payment(db_session, student, platform_kiosk) -> Payment:
    payment = open_checkout(
        db_session,
        FakeRazorpay(),
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("100.00"),
        receipt="ord_test",
        kiosk=platform_kiosk,
        credentials=Credentials("rzp_test", PLATFORM_SECRET),
        gateway=Gateway.PLATFORM_GATEWAY,
    )
    record_capture(db_session, payment, razorpay_payment_id="pay_REAL")
    return payment


def a_refund(
    db_session,
    payment,
    *,
    amount="20.00",
    destination=RefundDestination.WALLET,
    key="refund_1",
    razorpay=None,
    actor=None,
    reason=None,
    sink=None,
):
    return refund(
        db_session,
        payment=payment,
        amount=Decimal(amount),
        destination=destination,
        idempotency_key=key,
        actor_user_id=actor.id if actor is not None else None,
        reason=reason,
        razorpay=razorpay,
        # A gateway call needs keys, and none of these tests are about which.
        # `test_refund_credentials.py` is where that rule is pinned.
        credentials=SOME_KEYS if razorpay is not None else None,
        sink=sink,
    )


# ── the gate decides the destination ────────────────────────────────────────


def test_platform_money_may_go_to_the_wallet(db_session, platform_payment):
    row = a_refund(db_session, platform_payment)

    assert row.destination is RefundDestination.WALLET
    assert row.razorpay_refund_id is None
    assert platform_payment.refunded_inr == Decimal("20.00")
    assert platform_payment.status is PaymentStatus.PARTIALLY_REFUNDED


def test_platform_money_may_go_back_to_source(
    db_session, platform_payment
):
    gateway = FakeRazorpay()
    row = a_refund(
        db_session, platform_payment, destination=RefundDestination.SOURCE, razorpay=gateway
    )

    assert row.destination is RefundDestination.SOURCE
    assert row.razorpay_refund_id == "rfd_FAKE1"
    assert gateway.refunds[0]["amount_paise"] == 2000


@pytest.fixture
def owner(db_session):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.roles import Role

    user = User(email="collector@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    identity_repo.grant_role(db_session, user.id, Role.OWNER)
    return user


@pytest.fixture
def owner_payment(db_session, student, platform_kiosk, owner) -> Payment:
    """Money that landed in an owner's own Razorpay account."""
    payment = open_checkout(
        db_session,
        FakeRazorpay(),
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("100.00"),
        receipt="ord_owner",
        kiosk=platform_kiosk,
        credentials=Credentials("rzp_live_owner", "owner_secret"),
        gateway=Gateway.OWNER_GATEWAY,
        collecting_user_id=owner.id,
    )
    record_capture(db_session, payment, razorpay_payment_id="pay_OWNER")
    return payment


def test_owner_money_can_only_go_back_to_source(
    db_session, owner_payment
):
    """The illegal combination: asking for a wallet credit on money an owner
    collected. Refused, not silently honoured -- honouring it would move money
    out of an account the platform does not control."""
    with pytest.raises(BadRequest):
        a_refund(db_session, owner_payment, destination=RefundDestination.WALLET)

    assert owner_payment.refunded_inr == Decimal("0.00")
    assert owner_payment.status is PaymentStatus.CAPTURED


def test_owner_money_to_source_uses_the_live_connection(
    db_session, owner_payment
):
    gateway = FakeRazorpay()
    row = a_refund(
        db_session, owner_payment, destination=RefundDestination.SOURCE, razorpay=gateway
    )

    assert row.razorpay_refund_id == "rfd_FAKE1"
    assert gateway.refunds[0]["razorpay_payment_id"] == owner_payment.razorpay_payment_id


def test_an_owner_refund_with_no_gateway_is_refused(db_session, owner_payment):
    with pytest.raises(Conflict):
        a_refund(db_session, owner_payment, destination=RefundDestination.SOURCE)


# ── amounts ─────────────────────────────────────────────────────────────────


def test_a_refund_cannot_exceed_what_was_paid(db_session, platform_payment):
    with pytest.raises(Conflict):
        a_refund(db_session, platform_payment, amount="150.00")


def test_a_partial_refund_leaves_the_payment_partially_refunded(
    db_session, platform_payment
):
    a_refund(db_session, platform_payment, amount="30.00", key="r1")
    a_refund(db_session, platform_payment, amount="30.00", key="r2")

    assert platform_payment.refunded_inr == Decimal("60.00")
    assert platform_payment.status is PaymentStatus.PARTIALLY_REFUNDED


def test_a_full_refund_marks_the_payment_refunded(db_session, platform_payment):
    a_refund(db_session, platform_payment, amount="100.00", key="r_full")

    assert platform_payment.refunded_inr == Decimal("100.00")
    assert platform_payment.status is PaymentStatus.REFUNDED


def test_a_second_full_refund_is_refused(db_session, platform_payment):
    a_refund(db_session, platform_payment, amount="100.00", key="r_full")
    with pytest.raises(Conflict):
        a_refund(db_session, platform_payment, amount="1.00", key="r_extra")


def test_a_zero_or_negative_refund_is_refused(db_session, platform_payment):
    with pytest.raises(BadRequest):
        a_refund(db_session, platform_payment, amount="0.00")
    with pytest.raises(BadRequest):
        a_refund(db_session, platform_payment, amount="-5.00")


def test_a_refund_needs_a_captured_payment(db_session, student, platform_kiosk):
    """A payment that was opened but never paid cannot be refunded: there is
    nothing to give back."""
    payment = open_checkout(
        db_session,
        FakeRazorpay(),
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("100.00"),
        receipt="ord_uncaptured",
        kiosk=platform_kiosk,
        credentials=Credentials("rzp_test", PLATFORM_SECRET),
        gateway=Gateway.PLATFORM_GATEWAY,
    )
    assert payment.status is PaymentStatus.CREATED

    with pytest.raises(Conflict):
        a_refund(db_session, payment)


# ── idempotency ─────────────────────────────────────────────────────────────


def test_a_retried_refund_is_not_issued_twice(db_session, platform_payment):
    first = a_refund(
        db_session, platform_payment, razorpay=FakeRazorpay(), key="retry"
    )
    second = a_refund(
        db_session, platform_payment, razorpay=FakeRazorpay(), key="retry"
    )

    assert first.id == second.id
    assert platform_payment.refunded_inr == Decimal("20.00")
    assert platform_payment.status is PaymentStatus.PARTIALLY_REFUNDED


def test_the_same_key_returns_the_same_refund_not_a_second_one(
    db_session, platform_payment
):
    """A key is the caller's idempotency contract. Reusing it with a different
    amount is a caller mistake, and the safe answer is to hand back the refund
    that key already produced -- not to move another 5 rupees."""
    first = a_refund(db_session, platform_payment, amount="20.00", key="dup")
    second = a_refund(db_session, platform_payment, amount="5.00", key="dup")

    assert first.id == second.id
    assert platform_payment.refunded_inr == Decimal("20.00")


def test_distinct_keys_refund_distinctly(db_session, platform_payment):
    a_refund(db_session, platform_payment, amount="20.00", key="a")
    a_refund(db_session, platform_payment, amount="30.00", key="b")

    assert platform_payment.refunded_inr == Decimal("50.00")


# ── the wallet gets the money ───────────────────────────────────────────────


def test_a_wallet_refund_credits_the_student(db_session, platform_payment, student):
    from app.modules.wallet.ledger import balance_of

    a_refund(db_session, platform_payment, amount="20.00", key="wallet")

    assert balance_of(db_session, user_id=student.id) == Decimal("20.00")


def test_a_source_refund_does_not_touch_the_wallet(
    db_session, platform_payment, student
):
    from app.modules.wallet.ledger import balance_of

    a_refund(
        db_session,
        platform_payment,
        destination=RefundDestination.SOURCE,
        razorpay=FakeRazorpay(),
        key="source",
    )

    assert balance_of(db_session, user_id=student.id) == Decimal("0.00")


# ── the sink ────────────────────────────────────────────────────────────────


def test_a_refund_is_handled_to_whatever_the_payment_was_for(
    db_session, platform_payment
):
    """Payments does not import orders. A refund may close an order, and orders
    already imports payments, so the order transition comes in through this
    seam at the composition root. The protocol exists so the payment module
    only decides where the money goes."""
    sink = SpySink()
    a_refund(db_session, platform_payment, sink=sink, key="sink")

    assert len(sink.refunds) == 1
    assert sink.refunds[0].amount_inr == Decimal("20.00")


def test_a_refund_records_who_issued_it_and_why(
    db_session, platform_payment, student
):
    row = a_refund(
        db_session,
        platform_payment,
        actor=student,
        reason="document failed to print",
        key="operator",
    )

    assert row.actor_user_id == student.id
    assert row.reason == "document failed to print"


def test_a_long_reason_is_truncated(db_session, platform_payment):
    row = a_refund(
        db_session,
        platform_payment,
        reason="x" * 400,
        key="long",
    )
    assert row.reason is not None
    assert len(row.reason) <= 300


# ── the ledger entry is traceable ───────────────────────────────────────────


def test_a_wallet_refund_entry_carrys_its_own_reference(
    db_session, platform_payment, student
):
    from app.modules.wallet.ledger import statement

    a_refund(db_session, platform_payment, key="traceable")

    entries = statement(db_session, user_id=student.id)
    assert len(entries) == 1
    assert entries[0].kind is EntryKind.REFUND
    assert entries[0].reference == "refund:traceable"
    assert entries[0].amount_inr == Decimal("20.00")
    assert entries[0].balance_after_inr == Decimal("20.00")

# ── idempotency after the money has all gone back ───────────────────────────


def test_a_retried_full_refund_returns_the_first_one(db_session, platform_payment):
    """The case that matters most, and the one an amount check placed before the
    key lookup gets wrong. After a full refund the payment is REFUNDED with
    nothing left to give back, so a retry validated first is refused for
    exceeding the captured amount -- and the caller, told their refund failed
    when it had in fact succeeded, issues another one with a fresh key."""
    first = a_refund(db_session, platform_payment, amount="100.00", key="full_retry")
    second = a_refund(db_session, platform_payment, amount="100.00", key="full_retry")

    assert first.id == second.id
    assert platform_payment.refunded_inr == Decimal("100.00")
    assert platform_payment.status is PaymentStatus.REFUNDED


def test_a_retried_source_refund_is_not_issued_at_the_gateway_twice(
    db_session, platform_payment
):
    gateway = FakeRazorpay()
    a_refund(
        db_session,
        platform_payment,
        amount="100.00",
        destination=RefundDestination.SOURCE,
        razorpay=gateway,
        key="full_source_retry",
    )
    a_refund(
        db_session,
        platform_payment,
        amount="100.00",
        destination=RefundDestination.SOURCE,
        razorpay=gateway,
        key="full_source_retry",
    )

    assert len(gateway.refunds) == 1


def test_a_key_used_for_another_payment_is_refused(
    db_session, platform_payment, owner_payment
):
    """Handing back the other payment's refund row would report success for a
    refund that never happened against this one."""
    a_refund(db_session, platform_payment, amount="10.00", key="shared")

    with pytest.raises(Conflict):
        a_refund(
            db_session,
            owner_payment,
            amount="10.00",
            destination=RefundDestination.SOURCE,
            razorpay=FakeRazorpay(),
            key="shared",
        )

    assert owner_payment.refunded_inr == Decimal("0.00")


# ── the gateway is driven with our key ──────────────────────────────────────


def test_the_gateway_is_given_our_idempotency_key(db_session, platform_payment):
    """So that "already refunded" means the same thing on both sides."""
    gateway = FakeRazorpay()
    a_refund(
        db_session,
        platform_payment,
        destination=RefundDestination.SOURCE,
        razorpay=gateway,
        key="agreed",
    )

    assert gateway.refunds[0]["idempotency_key"] == "agreed"


# ── ids ─────────────────────────────────────────────────────────────────────


def test_a_refund_has_its_own_kind_of_id(db_session, platform_payment):
    """Not the payment's prefix. A refund id that parses as a payment id is the
    confusion that opaque prefixed ids exist to prevent."""
    from app.core.ids import IdPrefix, InvalidId, parse_id

    row = a_refund(db_session, platform_payment, key="ident")

    assert row.public_id.startswith("rfd_")
    parse_id(row.public_id, IdPrefix.REFUND)
    with pytest.raises(InvalidId):
        parse_id(row.public_id, IdPrefix.PAYMENT)


# ── the invariant, over a generated history ─────────────────────────────────


def test_refunds_never_exceed_what_was_captured(db_session, platform_payment):
    """Spec §9 names this as a property, not an example, and the distinction is
    the point: a single hand-picked over-refund proves one branch. Driven with
    arbitrary amounts, the sum of the refund rows and the payment's own
    `refunded_inr` must agree with each other and stay inside the captured
    amount at every step, whatever order the calls arrive in."""
    from app.modules.payments.refunds import refunds_for

    random.seed(20260820)
    captured = Decimal("100.00")

    for step in range(40):
        amount = Decimal(random.randrange(1, 4000)) / 100
        try:
            a_refund(db_session, platform_payment, amount=str(amount), key=f"p_{step}")
        except (BadRequest, Conflict):
            # Refusing an over-refund is itself part of the invariant: it must
            # leave nothing behind, not a partial write.
            pass

        rows = refunds_for(db_session, platform_payment)
        assert sum(r.amount_inr for r in rows) == platform_payment.refunded_inr
        assert platform_payment.refunded_inr <= captured

    assert rows, "the history refused every call and proved nothing"


def test_the_payment_status_always_matches_how_much_went_back(
    db_session, platform_payment
):
    """CAPTURED, PARTIALLY_REFUNDED and REFUNDED are not three flags somebody
    sets -- they are a function of `refunded_inr`. The old backend had
    `Payment.status` documented as three values and holding six, with two of
    them meaning success, precisely because each writer had its own opinion."""
    from app.modules.payments.refunds import refunds_for

    random.seed(7)
    for step in range(30):
        amount = Decimal(random.randrange(1, 2500)) / 100
        try:
            a_refund(db_session, platform_payment, amount=str(amount), key=f"s_{step}")
        except (BadRequest, Conflict):
            pass

        back = platform_payment.refunded_inr
        if back == Decimal("0.00"):
            expected = PaymentStatus.CAPTURED
        elif back < platform_payment.amount_inr:
            expected = PaymentStatus.PARTIALLY_REFUNDED
        else:
            expected = PaymentStatus.REFUNDED

        assert platform_payment.status is expected

    # The loop above proves nothing if every call was refused. Assert that money
    # actually moved -- three times in this build a "passing" check turned out
    # to be inspecting an empty set.
    assert refunds_for(db_session, platform_payment)

    # Random amounts stop short of the captured total rather than landing on it:
    # once the remainder is smaller than the next draw, every further call is
    # refused and `refunded_inr` never reaches `amount_inr`. So the REFUNDED
    # branch above is never taken, and the property is weaker than it reads.
    # Draining the remainder exactly is what makes it exercise all three states.
    remainder = platform_payment.amount_inr - platform_payment.refunded_inr
    assert remainder > Decimal("0.00")
    a_refund(db_session, platform_payment, amount=str(remainder), key="drain")

    assert platform_payment.refunded_inr == platform_payment.amount_inr
    assert platform_payment.status is PaymentStatus.REFUNDED


# ── a refused refund leaves a usable session ────────────────────────────────


def test_a_duplicate_refund_does_not_poison_the_transaction(
    db_session, platform_payment
):
    """The lesson the wallet module already learned, in a second place.

    A duplicate key raises IntegrityError from `flush()`, and that aborts the
    whole transaction -- not just the failed INSERT. Catching it and raising a
    tidy Conflict hands the caller a session on which every subsequent statement
    fails with "current transaction is aborted", a long way from the cause. The
    insert must happen inside a savepoint so rolling it back undoes exactly this
    refund and leaves everything else the request had done intact.
    """
    from sqlalchemy import select

    from app.modules.payments.models import Refund

    class Gateway:
        def refund(
            self, *, razorpay_payment_id, amount_paise, idempotency_key, credentials
        ):
            # The same gateway id twice: two of our rows claiming one real
            # refund, which the unique index refuses.
            return "rfnd_SAME"

    a_refund(
        db_session,
        platform_payment,
        destination=RefundDestination.SOURCE,
        razorpay=Gateway(),
        key="first",
    )

    with pytest.raises(Conflict):
        a_refund(
            db_session,
            platform_payment,
            destination=RefundDestination.SOURCE,
            razorpay=Gateway(),
            key="second",
        )

    # The session must still work. Without a savepoint this raises
    # PendingRollbackError instead of answering.
    assert db_session.execute(select(Refund)).scalars().all()
    assert platform_payment.refunded_inr == Decimal("20.00")
