"""Opening a Razorpay checkout, and recording what came back.

Two things are decided once here and never re-derived:

* **Whose account collects.** `kiosk_payment_gate` answers, and the answer is
  written onto the Payment row. The backend being replaced re-derived routing
  in three services, and two of them disagreeing is what sent student money to
  the wrong account.
* **Which key signs.** The credentials follow the gateway the gate chose, so a
  payment can never be created against the platform's account and verified
  against an owner's.

The gateway itself is behind a protocol. Tests exercise the whole flow without
a network, and the real client is one small class with the HTTP call in it --
which also means the security-critical part of this module, the signature check,
is never skipped in tests because "it needs Razorpay".
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.crypto import SecretBox
from app.core.errors import BadRequest, Conflict
from app.core.money import as_money, to_paise
from app.modules.kiosks import Kiosk
from app.modules.payments.configs import (
    decrypt_secret,
    decrypt_webhook_secret,
    get_config,
)
from app.modules.payments.gate import Gateway, kiosk_payment_gate
from app.modules.payments.models import Payment, PaymentKind, PaymentStatus
from app.modules.payments.signatures import verify_payment_signature

KIOSK_CLOSED = "This kiosk is not accepting payments at the moment."
NO_PLATFORM_KEYS = "Card payments are unavailable right now. Please try again later."
BAD_SIGNATURE = "We could not verify that payment. Nothing has been charged twice."
ALREADY_CAPTURED = "That payment has already been recorded."
WRONG_PAYMENT = "That payment does not belong to this order."


@dataclass(frozen=True)
class Credentials:
    """A Razorpay key pair. Never logged, never returned by an API."""

    key_id: str
    key_secret: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Credentials(key_id={self.key_id!r}, key_secret=***)"


class RazorpayGateway(Protocol):
    """The parts of Razorpay this module needs.

    A protocol rather than the SDK so the flow is testable without a network.
    """

    def create_order(
        self, *, amount_paise: int, receipt: str, credentials: Credentials
    ) -> str:
        """Open an order and return its razorpay_order_id."""
        ...


@dataclass(frozen=True)
class Collection:
    """Who collects here, with which keys, on whose behalf.

    One object because the three answers must never be paired from separate
    lookups. A caller that fetched the gateway and the credentials separately
    could act on a stale combination, which is precisely the class of bug that
    leaked money in the backend being replaced.
    """

    gateway: Gateway
    credentials: Credentials
    # The owner whose account collects, or None when it is the platform's.
    collecting_user_id: int | None


def credentials_for(
    db: Session,
    kiosk: Kiosk,
    *,
    box: SecretBox,
    platform_key_id: str,
    platform_key_secret: str,
) -> Collection:
    """The keys that must be used at this kiosk, and whose they are."""
    gateway = kiosk_payment_gate(db, kiosk)

    if gateway is Gateway.CLOSED:
        raise BadRequest(KIOSK_CLOSED)

    if gateway is Gateway.PLATFORM_GATEWAY:
        if not platform_key_id or not platform_key_secret:
            # Fail closed. Falling back to an owner's keys here would collect a
            # platform kiosk's takings into somebody else's account.
            raise BadRequest(NO_PLATFORM_KEYS)
        return Collection(
            gateway=gateway,
            credentials=Credentials(platform_key_id, platform_key_secret),
            collecting_user_id=None,
        )

    from app.modules.kiosks import repository as kiosk_repo

    owner = kiosk_repo.owner_of(db, kiosk)
    config = get_config(db, owner.id) if owner is not None else None
    if config is None:
        # Unreachable through the gate, which already checked for usable keys.
        # Kept because "unreachable" is a property of today's gate, and this is
        # the branch that would otherwise charge the wrong account.
        raise BadRequest(KIOSK_CLOSED)

    return Collection(
        gateway=gateway,
        credentials=Credentials(config.razorpay_key_id, decrypt_secret(config, box)),
        collecting_user_id=owner.id,
    )


def open_checkout(
    db: Session,
    razorpay: RazorpayGateway,
    *,
    user_id: int,
    kind: PaymentKind,
    amount: Decimal,
    receipt: str,
    kiosk: Kiosk | None,
    credentials: Credentials,
    gateway: Gateway,
    collecting_user_id: int | None = None,
    order_id: int | None = None,
) -> Payment:
    """Open a Razorpay order and record our side of it.

    The Payment row is written **before** the student is sent to pay, so a
    capture that arrives by webhook always has something to attach itself to.
    A row created on the way back would leave an unattributable payment whenever
    the student closed the tab.
    """
    amount = as_money(amount)
    if amount <= Decimal("0.00"):
        raise BadRequest("There is nothing to pay for.")

    razorpay_order_id = razorpay.create_order(
        amount_paise=to_paise(amount),
        receipt=receipt,
        credentials=credentials,
    )

    payment = Payment(
        user_id=user_id,
        kind=kind,
        order_id=order_id,
        kiosk_id=kiosk.id if kiosk is not None else None,
        gateway=gateway.value,
        collecting_user_id=collecting_user_id,
        razorpay_order_id=razorpay_order_id,
        amount_inr=amount,
        status=PaymentStatus.CREATED,
    )
    db.add(payment)
    db.flush()
    return payment


def payment_for_razorpay_order(db: Session, razorpay_order_id: str) -> Payment | None:
    return db.execute(
        select(Payment).where(Payment.razorpay_order_id == razorpay_order_id)
    ).scalar_one_or_none()


def confirm_payment(
    db: Session,
    payment: Payment,
    *,
    razorpay_payment_id: str,
    signature: str | None,
    key_secret: str,
    now: datetime | None = None,
) -> Payment:
    """Verify the browser's callback and record the capture.

    The signature is checked against the same key that opened the order. A
    payment that does not verify changes nothing at all -- no status, no id --
    so a forged callback cannot advance an order towards being printed.
    """
    if not verify_payment_signature(
        order_id=payment.razorpay_order_id,
        payment_id=razorpay_payment_id,
        signature=signature,
        secret=key_secret,
    ):
        raise BadRequest(BAD_SIGNATURE)

    return record_capture(db, payment, razorpay_payment_id=razorpay_payment_id, now=now)


def record_capture(
    db: Session,
    payment: Payment,
    *,
    razorpay_payment_id: str,
    now: datetime | None = None,
) -> Payment:
    """Mark a payment captured, exactly once.

    Both the browser callback and the webhook end up here, and either may
    arrive first or twice. The uniqueness of `razorpay_payment_id` is what makes
    the second one a refusal rather than a second capture -- the same mechanism
    the old backend used for top-ups and for nothing else.
    """
    now = now or datetime.now(UTC)

    if payment.status is PaymentStatus.CAPTURED:
        if payment.razorpay_payment_id == razorpay_payment_id:
            raise Conflict(ALREADY_CAPTURED)
        raise Conflict(WRONG_PAYMENT)

    payment.status = PaymentStatus.CAPTURED
    payment.razorpay_payment_id = razorpay_payment_id
    payment.captured_at = now
    db.add(payment)

    try:
        db.flush()
    except IntegrityError as exc:
        # The same razorpay_payment_id already sits on another row: a webhook
        # racing the callback. Refuse rather than let two of our rows claim one
        # real payment.
        raise Conflict(ALREADY_CAPTURED) from exc

    return payment


def record_failure(
    db: Session, payment: Payment, *, reason: str | None = None
) -> Payment:
    """A payment Razorpay told us did not succeed.

    Recorded rather than deleted: "the student tried and it failed" is a
    different fact from "the student never tried", and support needs both.
    """
    if payment.status is PaymentStatus.CAPTURED:
        # Money that has arrived cannot be un-arrived by a later failure event.
        raise Conflict(ALREADY_CAPTURED)

    payment.status = PaymentStatus.FAILED
    payment.failure_reason = (reason or "")[:300] or None
    db.add(payment)
    return payment


def webhook_secret_for(
    db: Session,
    *,
    collecting_user_id: int | None,
    box: SecretBox,
    platform_webhook_secret: str,
) -> str:
    """The secret that can verify a delivery arriving on this webhook URL.

    Owners register their own Razorpay webhook against their own account, so
    each has a URL naming them and a secret of their own. The URL is what
    selects the secret, before a byte of the body is parsed -- nothing an
    attacker sends influences which key their signature is checked against.

    Returns "" when there is nothing configured, and the signature check fails
    closed on an empty secret. An owner who has not set a webhook up has no
    verifiable deliveries, which is correct rather than exceptional.
    """
    if collecting_user_id is None:
        return platform_webhook_secret

    config = get_config(db, collecting_user_id)
    if config is None:
        return ""
    return decrypt_webhook_secret(config, box)
