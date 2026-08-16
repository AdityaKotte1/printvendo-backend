"""One webhook, one secret, one URL to register.

The backend being replaced had three: `/payments/webhook`,
`/subscription/webhook` and `/wallet/topup/webhook`. Three signature checks,
three ideas of what "already handled" meant, and three URLs that had to stay
registered and in step in the Razorpay dashboard. A payment is a payment; what
it was *for* is a field on our own row, not a different endpoint.

One handler, but **one URL per collecting account**. Owners collect into their
own Razorpay and register their own webhook there, so each has a URL naming
them and a signing secret of their own. The URL is what selects the secret --
chosen before a byte of the body is read, so nothing an attacker sends can
influence which key their signature is checked against.

Two properties this file exists to hold:

* **Nothing is parsed before the signature is checked.** The body is verified as
  raw bytes first. Parsing first and verifying the re-serialised result would
  let anyone change an amount and keep the signature.
* **A retry is a 200, not a 500.** Razorpay redelivers until it gets a success,
  so an already-handled event that raised would be redelivered forever. Every
  outcome here is something the endpoint can answer 200 to; only an unverified
  signature is a refusal.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.errors import Unauthorized
from app.modules.payments.charges import (
    payment_for_razorpay_order,
    record_capture,
    record_failure,
)
from app.modules.payments.models import Payment, PaymentKind, PaymentStatus

logger = logging.getLogger(__name__)

BAD_SIGNATURE = "That request could not be verified."
WRONG_ACCOUNT = "That payment was not collected by this account."

CAPTURED = "payment.captured"
FAILED = "payment.failed"

# Razorpay sends a great many event types. Anything not named here is
# acknowledged and ignored -- refusing unknown events would have Razorpay retry
# them forever, and a 500 on an event we simply do not care about is noise that
# hides the deliveries that matter.
HANDLED_EVENTS = frozenset({CAPTURED, FAILED})


class Settlement(Protocol):
    """What a captured payment means for the rest of the system.

    Implemented at the composition root, because settling a print order means
    calling the orders module and settling a top-up means calling the wallet --
    and payments must not import either, or the dependency graph acquires a
    cycle and the modules stop being separable.
    """

    def settle_print_order(self, db: Session, payment: Payment) -> None: ...

    def settle_wallet_topup(self, db: Session, payment: Payment) -> None: ...

    def settle_subscription(self, db: Session, payment: Payment) -> None: ...


@dataclass(frozen=True)
class WebhookOutcome:
    """What was done, for the endpoint to log and answer 200 to."""

    event: str
    handled: bool
    detail: str


def handle_webhook(
    db: Session,
    *,
    body: bytes,
    signature: str | None,
    secret: str,
    settlement: Settlement,
    collecting_user_id: int | None = None,
    now: datetime | None = None,
) -> WebhookOutcome:
    """Verify, understand, and settle a Razorpay event.

    `collecting_user_id` is whose webhook URL this arrived at -- None for the
    platform's. `secret` must be that account's signing secret; the caller
    resolves it from the URL with `charges.webhook_secret_for`.

    Raises only for an unverified signature, or for a verified delivery about
    somebody else's payment. Everything else -- an unknown event, an unknown
    order, a payment already captured -- is an outcome, so the endpoint answers
    200 and Razorpay stops redelivering.
    """
    from app.modules.payments.signatures import verify_webhook_signature

    if not verify_webhook_signature(body=body, signature=signature, secret=secret):
        # The one refusal. An unverified body has told us nothing, so there is
        # nothing to be idempotent about.
        raise Unauthorized(BAD_SIGNATURE)

    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        logger.warning("razorpay webhook: signed body was not JSON")
        return WebhookOutcome("", False, "unreadable body")

    event = payload.get("event") or ""
    if event not in HANDLED_EVENTS:
        return WebhookOutcome(event, False, "event not handled")

    entity = (
        payload.get("payload", {}).get("payment", {}).get("entity", {})
        if isinstance(payload.get("payload"), dict)
        else {}
    )
    razorpay_order_id = entity.get("order_id")
    razorpay_payment_id = entity.get("id")

    if not razorpay_order_id or not razorpay_payment_id:
        logger.warning("razorpay webhook: %s carried no ids", event)
        return WebhookOutcome(event, False, "event carried no order or payment id")

    payment = payment_for_razorpay_order(db, razorpay_order_id)
    if payment is None:
        # A payment we did not open. Acknowledged rather than refused: it may
        # belong to a different environment pointed at the same URL, and
        # retrying it forever helps nobody.
        logger.warning("razorpay webhook: no payment for %s", razorpay_order_id)
        return WebhookOutcome(event, False, "no such payment")

    if payment.collecting_user_id != collecting_user_id:
        # Verified, but about a payment collected by a different account. One
        # owner's webhook secret must not be able to settle another owner's
        # takings -- and with per-owner URLs an owner does hold a secret that
        # verifies, so this comparison is the only thing standing in the way.
        logger.warning(
            "razorpay webhook: %s delivered to the wrong account", razorpay_order_id
        )
        raise Unauthorized(WRONG_ACCOUNT)

    if event == FAILED:
        if payment.status is PaymentStatus.CAPTURED:
            # Events can arrive out of order. Money that has arrived cannot be
            # un-arrived by a late failure.
            return WebhookOutcome(event, False, "already captured")
        record_failure(db, payment, reason=entity.get("error_description"))
        return WebhookOutcome(event, True, "recorded as failed")

    if payment.status is PaymentStatus.CAPTURED:
        return WebhookOutcome(event, False, "already captured")

    record_capture(db, payment, razorpay_payment_id=razorpay_payment_id, now=now)
    _settle(db, payment, settlement)
    return WebhookOutcome(event, True, "captured and settled")


def _settle(db: Session, payment: Payment, settlement: Settlement) -> None:
    """Hand the captured payment to whatever it was for.

    A match on our own `kind` column rather than on the endpoint it arrived at.
    That is the whole difference from the three-webhook arrangement: the purpose
    is data we recorded when we opened the checkout, not a routing decision made
    by whoever configured the dashboard.
    """
    if payment.kind is PaymentKind.PRINT_ORDER:
        settlement.settle_print_order(db, payment)
    elif payment.kind is PaymentKind.WALLET_TOPUP:
        settlement.settle_wallet_topup(db, payment)
    elif payment.kind is PaymentKind.SUBSCRIPTION:
        settlement.settle_subscription(db, payment)
    else:  # pragma: no cover - the enum has no other members
        raise ValueError(f"No settlement for payment kind {payment.kind!r}")
