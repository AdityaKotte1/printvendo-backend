"""Reading and writing an owner's Razorpay credentials.

Three rules here are security properties:

* **The secret is encrypted at rest and never returned.** Reads give a masked
  key id and nothing else. `decrypt_secret` exists solely so the payment code
  can build a Razorpay client, and is not reachable from any route.
* **Keys can be set once.** Replacing them needs an admin-approved change
  request. Without that, taking over an owner's account silently redirects
  every student payment at every one of their kiosks.
* **A rejected or pending request grants nothing.** Only an APPROVED request is
  consumed, and consuming it marks it USED so one approval cannot authorise two
  changes.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import SecretBox, mask_secret
from app.core.errors import BadRequest, Conflict, NotFound
from app.modules.payments.models import (
    ChangeRequestStatus,
    KioskPaymentConfig,
    PaymentConfigChangeRequest,
)

ALREADY_CONFIGURED = (
    "Payment keys are already set for this account. Submit a change request to "
    "replace them."
)


@dataclass(frozen=True)
class PaymentConfigView:
    """What an API is allowed to say about someone's payment configuration."""

    is_configured: bool
    key_id_masked: str | None
    configured_at: datetime | None
    can_update: bool


def get_config(db: Session, user_id: int) -> KioskPaymentConfig | None:
    stmt = select(KioskPaymentConfig).where(KioskPaymentConfig.user_id == user_id)
    return db.execute(stmt).scalar_one_or_none()


def _approved_request(db: Session, user_id: int) -> PaymentConfigChangeRequest | None:
    stmt = select(PaymentConfigChangeRequest).where(
        PaymentConfigChangeRequest.user_id == user_id,
        PaymentConfigChangeRequest.status == ChangeRequestStatus.APPROVED,
    )
    return db.execute(stmt).scalars().first()


def view_config(db: Session, user_id: int) -> PaymentConfigView:
    """What the owner app shows. Contains no secret, by construction."""
    config = get_config(db, user_id)

    if config is None or not config.is_configured:
        return PaymentConfigView(
            is_configured=False,
            key_id_masked=None,
            configured_at=None,
            can_update=True,
        )

    return PaymentConfigView(
        is_configured=True,
        key_id_masked=mask_secret(config.razorpay_key_id or ""),
        configured_at=config.configured_at,
        can_update=_approved_request(db, user_id) is not None,
    )


def set_keys(
    db: Session,
    user_id: int,
    *,
    key_id: str,
    key_secret: str,
    box: SecretBox,
) -> KioskPaymentConfig:
    """Store an owner's Razorpay credentials, encrypting the secret.

    Allowed when nothing is configured yet, or when an approved change request
    is waiting -- which is then consumed, so one approval authorises exactly one
    change.
    """
    key_id = key_id.strip()
    key_secret = key_secret.strip()

    if not key_id or not key_secret:
        raise BadRequest("Both the Razorpay key id and key secret are required.")

    config = get_config(db, user_id)

    if config is not None and config.is_configured:
        approved = _approved_request(db, user_id)
        if approved is None:
            raise Conflict(ALREADY_CONFIGURED)

        approved.status = ChangeRequestStatus.USED
        db.add(approved)

    if config is None:
        config = KioskPaymentConfig(user_id=user_id)
        db.add(config)

    config.razorpay_key_id = key_id
    config.razorpay_key_secret_encrypted = box.encrypt(key_secret)
    config.is_configured = True
    config.configured_at = datetime.now(UTC)
    db.add(config)

    return config


def set_webhook_secret(
    db: Session, user_id: int, *, webhook_secret: str, box: SecretBox
) -> KioskPaymentConfig:
    """Store the signing secret for this owner's own Razorpay webhook.

    Separate from the API key secret because Razorpay treats it separately: it
    is set per webhook in their dashboard, not per key, and an owner can rotate
    one without the other.

    Deliberately **not** behind the set-once-with-approval flow that guards the
    keys. Rotating a webhook secret cannot redirect anybody's money -- the worst
    it can do is make that owner's own deliveries stop verifying, which they
    will notice immediately and can fix themselves. Requiring an admin for it
    would mean an owner whose secret leaked has to wait to close the hole.
    """
    if not webhook_secret or not webhook_secret.strip():
        raise BadRequest("Enter the webhook secret from your Razorpay dashboard.")

    config = get_config(db, user_id)
    if config is None:
        config = KioskPaymentConfig(user_id=user_id)
        db.add(config)

    config.razorpay_webhook_secret_encrypted = box.encrypt(webhook_secret.strip())
    db.add(config)
    db.flush()
    return config


def decrypt_webhook_secret(config: KioskPaymentConfig, box: SecretBox) -> str:
    """This owner's webhook signing secret, or "" if they have not set one.

    Empty rather than None so it flows straight into the signature check, which
    fails closed on an empty secret. An owner who has not set one up simply has
    no verifiable deliveries -- which is correct, and is not an exception.
    """
    if not config.razorpay_webhook_secret_encrypted:
        return ""
    return box.decrypt(config.razorpay_webhook_secret_encrypted)


def decrypt_secret(config: KioskPaymentConfig, box: SecretBox) -> str:
    """The owner's Razorpay secret, for building a client to charge with.

    Deliberately not reachable from any route: nothing an owner or admin can
    call returns this. It exists for the payment path only.
    """
    if not config.razorpay_key_secret_encrypted:
        raise NotFound("This account has no payment keys configured.")
    return box.decrypt(config.razorpay_key_secret_encrypted)


def has_usable_keys(db: Session, user_id: int) -> bool:
    """Whether this owner can currently collect into their own account.

    One of the two halves of the payment gate -- the other is an active
    subscription.
    """
    config = get_config(db, user_id)
    return bool(
        config
        and config.is_configured
        and config.razorpay_key_id
        and config.razorpay_key_secret_encrypted
    )


def request_change(
    db: Session, user_id: int, *, reason: str | None, proof_path: str | None
) -> PaymentConfigChangeRequest:
    """Ask an admin for permission to replace payment keys."""
    existing = db.execute(
        select(PaymentConfigChangeRequest).where(
            PaymentConfigChangeRequest.user_id == user_id,
            PaymentConfigChangeRequest.status == ChangeRequestStatus.PENDING,
        )
    ).scalars().first()
    if existing is not None:
        raise Conflict("You already have a change request awaiting review.")

    request = PaymentConfigChangeRequest(
        user_id=user_id, reason=reason, proof_path=proof_path
    )
    db.add(request)
    return request


def review_change(
    db: Session,
    request: PaymentConfigChangeRequest,
    *,
    approve: bool,
    reviewer_user_id: int,
    note: str | None = None,
) -> PaymentConfigChangeRequest:
    """Approve or reject a pending request.

    Only a PENDING request can be reviewed; re-approving a USED one would hand
    out a second change from one decision.
    """
    if request.status is not ChangeRequestStatus.PENDING:
        raise Conflict(f"That request has already been {request.status.value}.")

    request.status = (
        ChangeRequestStatus.APPROVED if approve else ChangeRequestStatus.REJECTED
    )
    request.reviewed_by_user_id = reviewer_user_id
    request.reviewed_at = datetime.now(UTC)
    request.review_note = note
    db.add(request)
    return request
