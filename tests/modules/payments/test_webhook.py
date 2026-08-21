"""The one webhook.

The backend being replaced had three, each with its own signature check and its
own idea of what "already handled" meant. These tests hold the two properties
that matter: nothing is trusted before the signature is verified, and a
redelivery is answered rather than raised.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.core.errors import Unauthorized
from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk
from app.modules.payments.charges import Credentials, open_checkout
from app.modules.payments.gate import Gateway
from app.modules.payments.models import Payment, PaymentKind, PaymentStatus
from app.modules.payments.webhook import handle_webhook

SECRET = "webhook_secret_value"


@dataclass
class SpySettlement:
    """Records what a captured payment was handed to."""

    print_orders: list[Payment] = field(default_factory=list)
    topups: list[Payment] = field(default_factory=list)
    subscriptions: list[Payment] = field(default_factory=list)

    def settle_print_order(self, db, payment):
        self.print_orders.append(payment)

    def settle_wallet_topup(self, db, payment):
        self.topups.append(payment)

    def settle_subscription(self, db, payment):
        self.subscriptions.append(payment)


class FakeRazorpay:
    def __init__(self) -> None:
        self.counter = 0

    def create_order(self, *, amount_paise, receipt, credentials) -> str:
        self.counter += 1
        return f"order_FAKE{self.counter}"


@pytest.fixture
def settlement() -> SpySettlement:
    return SpySettlement()


@pytest.fixture
def student(db_session):
    user = User(email="hook@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def kiosk(db_session):
    kiosk = Kiosk(
        name="Webhook Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
    )
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def payment(db_session, student, kiosk) -> Payment:
    return open_checkout(
        db_session,
        FakeRazorpay(),
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("20.00"),
        receipt="ord_test",
        kiosk=kiosk,
        credentials=Credentials("rzp_test", "secret"),
        gateway=Gateway.PLATFORM_GATEWAY,
    )


def event_body(
    *, event: str = "payment.captured", order_id: str, payment_id: str = "pay_REAL", **extra
) -> bytes:
    entity = {"id": payment_id, "order_id": order_id, **extra}
    return json.dumps(
        {"event": event, "payload": {"payment": {"entity": entity}}}
    ).encode()


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


UNSET = object()


def deliver(
    db_session,
    settlement,
    body: bytes,
    signature=UNSET,
    *,
    secret: str = SECRET,
    collecting_user_id: int | None = None,
):
    """Deliver an event. Omit `signature` to have it signed correctly.

    A sentinel rather than a None default, because "no signature header at all"
    is one of the cases under test and must not be silently signed for.
    """
    return handle_webhook(
        db_session,
        body=body,
        signature=sign(body, secret) if signature is UNSET else signature,
        secret=secret,
        settlement=settlement,
        collecting_user_id=collecting_user_id,
    )


# ── nothing is trusted before the signature ─────────────────────────────────


def test_an_unsigned_delivery_is_refused(db_session, settlement, payment):
    body = event_body(order_id=payment.razorpay_order_id)

    with pytest.raises(Unauthorized):
        deliver(db_session, settlement, body, signature=None)

    assert payment.status is PaymentStatus.CREATED
    assert settlement.print_orders == []


def test_a_forged_delivery_settles_nothing(db_session, settlement, payment):
    """The whole attack: a body claiming a payment was captured. Without a valid
    signature it must not reach the order, the wallet, or a printer."""
    body = event_body(order_id=payment.razorpay_order_id)

    with pytest.raises(Unauthorized):
        deliver(db_session, settlement, body, signature="ff" * 32)

    assert payment.status is PaymentStatus.CREATED
    assert settlement.print_orders == []


def test_a_tampered_body_is_refused(db_session, settlement, payment):
    body = event_body(order_id=payment.razorpay_order_id)
    signature = sign(body)
    tampered = body.replace(b"pay_REAL", b"pay_FAKE")

    with pytest.raises(Unauthorized):
        deliver(db_session, settlement, tampered, signature=signature)


def test_a_signed_body_that_is_not_json_is_acknowledged_not_raised(
    db_session, settlement
):
    """Signed, so it really came from Razorpay -- but unreadable. Raising would
    have it redelivered forever."""
    outcome = deliver(db_session, settlement, b"not json at all")

    assert outcome.handled is False


# ── capture ─────────────────────────────────────────────────────────────────


def test_a_captured_payment_is_recorded_and_settled(db_session, settlement, payment):
    outcome = deliver(
        db_session, settlement, event_body(order_id=payment.razorpay_order_id)
    )

    assert outcome.handled is True
    assert payment.status is PaymentStatus.CAPTURED
    assert payment.razorpay_payment_id == "pay_REAL"
    assert settlement.print_orders == [payment]


def test_settlement_follows_our_own_record_of_what_it_was_for(
    db_session, settlement, student
):
    """A top-up and a print order arrive at the same URL and are told apart by
    the `kind` we wrote when opening the checkout -- not by which endpoint they
    hit, which is how the old backend decided."""
    topup = open_checkout(
        db_session,
        FakeRazorpay(),
        user_id=student.id,
        kind=PaymentKind.WALLET_TOPUP,
        amount=Decimal("100.00"),
        receipt="topup",
        kiosk=None,
        credentials=Credentials("rzp_test", "secret"),
        gateway=Gateway.PLATFORM_GATEWAY,
    )

    deliver(
        db_session,
        settlement,
        event_body(order_id=topup.razorpay_order_id, payment_id="pay_TOPUP"),
    )

    assert settlement.topups == [topup]
    assert settlement.print_orders == []


def test_a_redelivery_settles_once(db_session, settlement, payment):
    """Razorpay redelivers until it gets a 200. The second delivery must not
    print a second time, and must not raise."""
    body = event_body(order_id=payment.razorpay_order_id)
    deliver(db_session, settlement, body)

    outcome = deliver(db_session, settlement, body)

    assert outcome.handled is False
    assert len(settlement.print_orders) == 1


def test_an_event_for_a_payment_we_never_opened_is_acknowledged(
    db_session, settlement
):
    """Possibly another environment pointed at the same URL. Retrying it forever
    helps nobody."""
    outcome = deliver(db_session, settlement, event_body(order_id="order_UNKNOWN"))

    assert outcome.handled is False
    assert settlement.print_orders == []


def test_an_event_with_no_ids_is_acknowledged(db_session, settlement):
    body = json.dumps({"event": "payment.captured", "payload": {}}).encode()

    outcome = deliver(db_session, settlement, body)

    assert outcome.handled is False


def test_an_event_we_do_not_handle_is_acknowledged(db_session, settlement, payment):
    """Razorpay sends dozens of event types. Refusing them would have every one
    redelivered forever."""
    outcome = deliver(
        db_session,
        settlement,
        event_body(event="payment.authorized", order_id=payment.razorpay_order_id),
    )

    assert outcome.handled is False
    assert payment.status is PaymentStatus.CREATED


# ── failure ─────────────────────────────────────────────────────────────────


def test_a_failed_payment_is_recorded(db_session, settlement, payment):
    outcome = deliver(
        db_session,
        settlement,
        event_body(
            event="payment.failed",
            order_id=payment.razorpay_order_id,
            error_description="card declined",
        ),
    )

    assert outcome.handled is True
    assert payment.status is PaymentStatus.FAILED
    assert payment.failure_reason == "card declined"
    assert settlement.print_orders == []


def test_a_late_failure_cannot_undo_a_capture(db_session, settlement, payment):
    """Razorpay events are not ordered. A failure arriving after a capture must
    leave the capture -- and the print -- alone."""
    deliver(db_session, settlement, event_body(order_id=payment.razorpay_order_id))

    outcome = deliver(
        db_session,
        settlement,
        event_body(event="payment.failed", order_id=payment.razorpay_order_id),
    )

    assert outcome.handled is False
    assert payment.status is PaymentStatus.CAPTURED
    assert len(settlement.print_orders) == 1


# ── one URL per collecting account ──────────────────────────────────────────


@pytest.fixture
def owner(db_session):
    from app.modules.identity import repository as identity_repo
    from app.modules.identity.roles import Role

    owner = User(email="collector@example.com", hashed_password="x")
    db_session.add(owner)
    db_session.flush()
    identity_repo.grant_role(db_session, owner.id, Role.OWNER)
    return owner


@pytest.fixture
def owner_collected_payment(db_session, student, kiosk, owner) -> Payment:
    """A payment that landed in an owner's own Razorpay account."""
    return open_checkout(
        db_session,
        FakeRazorpay(),
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=Decimal("20.00"),
        receipt="ord_owner",
        kiosk=kiosk,
        credentials=Credentials("rzp_live_owner", "owner_secret"),
        gateway=Gateway.OWNER_GATEWAY,
        collecting_user_id=owner.id,
    )


def test_a_delivery_settles_only_at_the_url_of_the_account_that_collected(
    db_session, settlement, owner, owner_collected_payment
):
    """Owners register their own webhooks, so an owner holds a secret that
    genuinely verifies. Nothing but this check stops them delivering a signed
    event about a *competitor's* takings to their own URL and settling it."""
    body = event_body(order_id=owner_collected_payment.razorpay_order_id)

    with pytest.raises(Unauthorized):
        deliver(db_session, settlement, body, collecting_user_id=owner.id + 999)

    assert owner_collected_payment.status is PaymentStatus.CREATED
    assert settlement.print_orders == []


def test_the_platform_url_cannot_settle_an_owners_takings(
    db_session, settlement, owner_collected_payment
):
    body = event_body(order_id=owner_collected_payment.razorpay_order_id)

    with pytest.raises(Unauthorized):
        deliver(db_session, settlement, body, collecting_user_id=None)

    assert owner_collected_payment.status is PaymentStatus.CREATED


def test_an_owner_url_cannot_settle_a_platform_payment(
    db_session, settlement, owner, payment
):
    body = event_body(order_id=payment.razorpay_order_id)

    with pytest.raises(Unauthorized):
        deliver(db_session, settlement, body, collecting_user_id=owner.id)

    assert payment.status is PaymentStatus.CREATED


def test_an_owners_own_delivery_settles_normally(
    db_session, settlement, owner, owner_collected_payment
):
    body = event_body(order_id=owner_collected_payment.razorpay_order_id)

    outcome = deliver(db_session, settlement, body, collecting_user_id=owner.id)

    assert outcome.handled is True
    assert owner_collected_payment.status is PaymentStatus.CAPTURED
    assert settlement.print_orders == [owner_collected_payment]


# ── which secret verifies a delivery ────────────────────────────────────────


def test_the_platform_url_verifies_with_the_platform_secret(db_session):
    from app.core.crypto import SecretBox
    from app.modules.payments.charges import webhook_secret_for

    resolved = webhook_secret_for(
        db_session,
        collecting_user_id=None,
        box=SecretBox("k" * 43 + "="),
        platform_webhook_secret="the_platform_webhook_secret",
    )

    assert resolved == "the_platform_webhook_secret"


def test_an_owner_url_verifies_with_that_owners_secret(db_session, owner):
    """Stored encrypted, decrypted only to check a signature -- it never travels
    back out through an API the way the old plaintext key secret could."""
    from app.core.crypto import SecretBox
    from app.modules.payments.charges import webhook_secret_for
    from app.modules.payments.configs import set_webhook_secret

    box = SecretBox("k" * 43 + "=")
    config = set_webhook_secret(
        db_session, owner.id, webhook_secret="owners_hook_secret", box=box
    )

    assert "owners_hook_secret" not in (config.razorpay_webhook_secret_encrypted or "")
    assert (
        webhook_secret_for(
            db_session,
            collecting_user_id=owner.id,
            box=box,
            platform_webhook_secret="the_platform_webhook_secret",
        )
        == "owners_hook_secret"
    )


def test_an_owner_who_has_not_set_a_webhook_up_verifies_nothing(db_session, owner):
    """Empty, not the platform's. Falling back to the platform secret would let
    the platform's webhook settle an owner's takings, which is the confusion
    this whole arrangement exists to avoid."""
    from app.core.crypto import SecretBox
    from app.modules.payments.charges import webhook_secret_for

    assert (
        webhook_secret_for(
            db_session,
            collecting_user_id=owner.id,
            box=SecretBox("k" * 43 + "="),
            platform_webhook_secret="the_platform_webhook_secret",
        )
        == ""
    )


def test_a_delivery_to_an_unconfigured_owner_url_is_refused(
    db_session, settlement, owner, owner_collected_payment
):
    """End to end: no secret means no verifiable delivery, and the signature
    check fails closed on an empty secret rather than accepting anything."""
    body = event_body(order_id=owner_collected_payment.razorpay_order_id)

    with pytest.raises(Unauthorized):
        deliver(
            db_session,
            settlement,
            body,
            secret="",
            collecting_user_id=owner.id,
        )


# ── refunds issued somewhere else ───────────────────────────────────────────
#
# An owner can refund a student from their own Razorpay dashboard, and the
# platform can do the same from its own. Without this the money leaves the
# account and our books never hear: the Payment stays CAPTURED and the order
# stays PAID, so the student is refunded and still owed a print.


@dataclass
class SpyRefundSink:
    refunds: list = field(default_factory=list)

    def on_refund(self, db, payment, refund):
        self.refunds.append(refund)


def refund_body(
    *,
    event: str = "refund.processed",
    order_id: str,
    payment_id: str = "pay_REAL",
    refund_id: str = "rfnd_EXTERNAL",
    amount_paise: int = 2000,
) -> bytes:
    """Razorpay puts both entities in a refund event, so the payment is found
    the same way as for a capture -- one lookup path, and the wrong-account
    check keeps working unchanged."""
    return json.dumps(
        {
            "event": event,
            "payload": {
                "refund": {
                    "entity": {
                        "id": refund_id,
                        "payment_id": payment_id,
                        "amount": amount_paise,
                        "status": "processed",
                    }
                },
                "payment": {"entity": {"id": payment_id, "order_id": order_id}},
            },
        }
    ).encode()


@pytest.fixture
def captured(db_session, settlement, payment) -> Payment:
    deliver(db_session, settlement, event_body(order_id=payment.razorpay_order_id))
    return payment


def test_a_refund_made_at_razorpay_is_recorded_here(db_session, settlement, captured):
    from app.modules.payments.refunds import refunds_for

    sink = SpyRefundSink()
    outcome = handle_webhook(
        db_session,
        body=(body := refund_body(order_id=captured.razorpay_order_id)),
        signature=sign(body),
        secret=SECRET,
        settlement=settlement,
        refund_sink=sink,
    )

    assert outcome.handled is True
    rows = refunds_for(db_session, captured)
    assert len(rows) == 1
    assert rows[0].razorpay_refund_id == "rfnd_EXTERNAL"
    assert rows[0].amount_inr == Decimal("20.00")
    assert captured.refunded_inr == Decimal("20.00")
    assert captured.status is PaymentStatus.REFUNDED
    assert len(sink.refunds) == 1


def test_a_redelivered_refund_event_records_one_refund(
    db_session, settlement, captured
):
    """Razorpay redelivers. The refund id is what makes the second delivery a
    no-op, exactly as razorpay_payment_id does for a capture."""
    from app.modules.payments.refunds import refunds_for

    body = refund_body(order_id=captured.razorpay_order_id)
    outcomes = [
        handle_webhook(
            db_session,
            body=body,
            signature=sign(body),
            secret=SECRET,
            settlement=settlement,
            refund_sink=SpyRefundSink(),
        ).detail
        for _ in range(3)
    ]

    assert len(refunds_for(db_session, captured)) == 1
    assert captured.refunded_inr == Decimal("20.00")
    assert outcomes[1:] == ["refund already recorded", "refund already recorded"]


def test_a_partial_refund_at_razorpay_is_recorded_as_partial(
    db_session, settlement, captured
):
    body = refund_body(order_id=captured.razorpay_order_id, amount_paise=500)
    handle_webhook(
        db_session,
        body=body,
        signature=sign(body),
        secret=SECRET,
        settlement=settlement,
        refund_sink=SpyRefundSink(),
    )

    assert captured.refunded_inr == Decimal("5.00")
    assert captured.status is PaymentStatus.PARTIALLY_REFUNDED


def test_a_refund_we_issued_ourselves_is_not_recorded_twice(
    db_session, settlement, captured
):
    """Our own to-source refund produces a refund.processed event too. The
    razorpay refund id we stored when we issued it is what recognises it."""
    from app.modules.payments import RefundDestination, refund
    from app.modules.payments.refunds import refunds_for

    class Gateway:
        def refund(
            self, *, razorpay_payment_id, amount_paise, idempotency_key, credentials
        ):
            return "rfnd_OURS"

    refund(
        db_session,
        payment=captured,
        amount=Decimal("20.00"),
        destination=RefundDestination.SOURCE,
        idempotency_key="ours",
        razorpay=Gateway(),
        credentials=Credentials("rzp_test", "secret"),
    )

    body = refund_body(order_id=captured.razorpay_order_id, refund_id="rfnd_OURS")
    outcome = handle_webhook(
        db_session,
        body=body,
        signature=sign(body),
        secret=SECRET,
        settlement=settlement,
        refund_sink=SpyRefundSink(),
    )

    # "already recorded", not "could not be recorded". The distinction is the
    # point: without the razorpay-id lookup this still ends up as one refund,
    # but by attempting an insert that the unique index refuses -- so the log
    # reads like a failure and an operator is sent looking for a problem that
    # does not exist.
    assert outcome.handled is False
    assert outcome.detail == "refund already recorded"
    assert len(refunds_for(db_session, captured)) == 1
    assert captured.refunded_inr == Decimal("20.00")


def test_a_refund_event_for_another_owners_payment_is_refused(
    db_session, settlement, captured
):
    """The same rule as for a capture, and it must not be weaker here: an owner
    holds a secret that verifies, so this comparison is the only thing stopping
    them writing refunds against a competitor's takings."""
    body = refund_body(order_id=captured.razorpay_order_id)

    with pytest.raises(Unauthorized):
        handle_webhook(
            db_session,
            body=body,
            signature=sign(body),
            secret=SECRET,
            settlement=settlement,
            refund_sink=SpyRefundSink(),
            collecting_user_id=999_999,
        )

    assert captured.refunded_inr == Decimal("0.00")


def test_an_unsigned_refund_event_changes_nothing(db_session, settlement, captured):
    body = refund_body(order_id=captured.razorpay_order_id)

    with pytest.raises(Unauthorized):
        handle_webhook(
            db_session,
            body=body,
            signature="ff" * 32,
            secret=SECRET,
            settlement=settlement,
            refund_sink=SpyRefundSink(),
        )

    assert captured.refunded_inr == Decimal("0.00")


def test_a_refund_created_event_is_not_money_yet(db_session, settlement, captured):
    """`refund.created` means Razorpay accepted the instruction, not that the
    student has the money. Recording it would show a refund that can still
    fail."""
    body = refund_body(order_id=captured.razorpay_order_id, event="refund.created")
    outcome = handle_webhook(
        db_session,
        body=body,
        signature=sign(body),
        secret=SECRET,
        settlement=settlement,
        refund_sink=SpyRefundSink(),
    )

    assert outcome.handled is False
    assert captured.refunded_inr == Decimal("0.00")
