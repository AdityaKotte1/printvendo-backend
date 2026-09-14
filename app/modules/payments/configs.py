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
from sqlalchemy.orm import Session, aliased

from app.core.crypto import SecretBox, mask_secret
from app.core.errors import BadRequest, Conflict, NotFound
from app.modules.identity import User
from app.modules.payments.models import (
    ChangeRequestStatus,
    KioskPaymentConfig,
    PaymentConfigChangeRequest,
)

NO_SUCH_REQUEST = "That change request does not exist."
NO_PROOF = "No proof of account ownership was uploaded with that request."

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


def _config_view(
    config: KioskPaymentConfig | None, *, change_approved: bool
) -> PaymentConfigView:
    """The one place a stored configuration becomes something an API may say.

    The owner's own page and the admin console's list both come through here,
    so neither can show more of a key than the other.
    """
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
        can_update=change_approved,
    )


def view_config(db: Session, user_id: int) -> PaymentConfigView:
    """What the owner app shows. Contains no secret, by construction."""
    config = get_config(db, user_id)
    configured = config is not None and config.is_configured
    return _config_view(
        config,
        change_approved=configured and _approved_request(db, user_id) is not None,
    )


@dataclass(frozen=True)
class ConfiguredOwnerView:
    """One account's payment keys, as an admin may see them.

    `keys` is exactly what the owner's own page shows -- see `_config_view`.
    Whether a webhook secret is set is a yes or a no; what it is never leaves.
    """

    owner_public_id: str
    owner_email: str
    owner_name: str | None
    keys: PaymentConfigView
    has_webhook_secret: bool


def configured_owners(db: Session, *, limit: int = 500) -> list[ConfiguredOwnerView]:
    """Every account collecting into its own Razorpay, most recently set first.

    Two queries however many there are: the accounts with their owners joined
    in, then which of them hold an approved change not yet used. Calling
    `view_config` per row would be the legacy audit's N+1 in a new place.
    """
    rows = db.execute(
        select(KioskPaymentConfig, User)
        .join(User, User.id == KioskPaymentConfig.user_id)
        .where(KioskPaymentConfig.is_configured.is_(True))
        .order_by(
            KioskPaymentConfig.configured_at.desc().nulls_last(),
            KioskPaymentConfig.id.desc(),
        )
        .limit(limit)
    ).all()
    if not rows:
        return []

    approved = set(
        db.execute(
            select(PaymentConfigChangeRequest.user_id).where(
                PaymentConfigChangeRequest.user_id.in_([c.user_id for c, _ in rows]),
                PaymentConfigChangeRequest.status == ChangeRequestStatus.APPROVED,
            )
        ).scalars()
    )
    return [
        ConfiguredOwnerView(
            owner_public_id=owner.public_id,
            owner_email=owner.email,
            owner_name=owner.full_name,
            keys=_config_view(config, change_approved=config.user_id in approved),
            has_webhook_secret=bool(config.razorpay_webhook_secret_encrypted),
        )
        for config, owner in rows
    ]


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


@dataclass(frozen=True)
class ChangeRequestView:
    """One request, as an admin reviewing it may see it.

    Carries `has_proof` rather than the storage key. A key in a JSON response is
    an invitation to build a URL out of it, and that is precisely the mistake
    this loop exists to prevent -- the old admin dashboard concatenated
    `API_BASE + '/storage/...'`, which 404s silently behind an `onerror`
    handler, so proofs were approved unseen. The bytes come from an
    authenticated route that takes `public_id`, and nothing else can address
    the file.
    """

    public_id: str
    owner_public_id: str
    owner_email: str
    reason: str | None
    has_proof: bool
    status: ChangeRequestStatus
    created_at: datetime
    reviewed_at: datetime | None
    review_note: str | None
    # Who decided, by address. None while it waits.
    reviewed_by_email: str | None = None


def _view(
    request: PaymentConfigChangeRequest,
    owner: User,
    *,
    reviewed_by_email: str | None = None,
) -> ChangeRequestView:
    return ChangeRequestView(
        public_id=request.public_id,
        owner_public_id=owner.public_id,
        owner_email=owner.email,
        reason=request.reason,
        has_proof=bool(request.proof_path),
        status=ChangeRequestStatus(request.status),
        created_at=request.created_at,
        reviewed_at=request.reviewed_at,
        review_note=request.review_note,
        reviewed_by_email=reviewed_by_email,
    )


def pending_change_requests(db: Session, *, limit: int = 100) -> list[ChangeRequestView]:
    """The review queue, oldest first.

    Oldest first on purpose, unlike every other listing here: this is a worklist
    somebody works through, and an owner whose takings are misrouted is waiting
    on it. Newest-first would bury the person who has waited longest.

    The owner is joined in rather than looked up per row -- a review queue that
    costs one query per pending request is the legacy audit's N+1 in a new
    place.
    """
    rows = db.execute(
        select(PaymentConfigChangeRequest, User)
        .join(User, User.id == PaymentConfigChangeRequest.user_id)
        .where(PaymentConfigChangeRequest.status == ChangeRequestStatus.PENDING)
        .order_by(PaymentConfigChangeRequest.created_at, PaymentConfigChangeRequest.id)
        .limit(limit)
    ).all()
    return [_view(request, owner) for request, owner in rows]


def change_request_history(db: Session, *, limit: int = 200) -> list[ChangeRequestView]:
    """Every request, whatever became of it, newest first.

    The queue answers "what is waiting"; this answers "who changed where this
    shop's money goes, when, and on whose say-so" -- the question asked after
    somebody's takings go missing. Newest first because it is read as a
    history, not worked through as a list.

    The reviewer is joined in beside the owner, so a page costs one query.
    """
    reviewer = aliased(User)
    rows = db.execute(
        select(PaymentConfigChangeRequest, User, reviewer.email)
        .join(User, User.id == PaymentConfigChangeRequest.user_id)
        .outerjoin(reviewer, reviewer.id == PaymentConfigChangeRequest.reviewed_by_user_id)
        .order_by(
            PaymentConfigChangeRequest.created_at.desc(),
            PaymentConfigChangeRequest.id.desc(),
        )
        .limit(limit)
    ).all()
    return [
        _view(request, owner, reviewed_by_email=email) for request, owner, email in rows
    ]


def _by_public_id(db: Session, public_id: str) -> PaymentConfigChangeRequest:
    request = db.execute(
        select(PaymentConfigChangeRequest).where(
            PaymentConfigChangeRequest.public_id == public_id
        )
    ).scalar_one_or_none()
    if request is None:
        raise NotFound(NO_SUCH_REQUEST)
    return request


def review_change_by_id(
    db: Session,
    public_id: str,
    *,
    approve: bool,
    reviewer_user_id: int,
    note: str | None = None,
) -> ChangeRequestView:
    """Approve or reject the request with this public id.

    A thin wrapper over `review_change`, which keeps the rules -- so the api
    layer never has to hold a row, and there is still exactly one place that
    decides whether a request may be reviewed.
    """
    request = _by_public_id(db, public_id)
    review_change(
        db, request, approve=approve, reviewer_user_id=reviewer_user_id, note=note
    )
    db.flush()

    owner = db.execute(select(User).where(User.id == request.user_id)).scalar_one()
    reviewer = db.execute(
        select(User.email).where(User.id == reviewer_user_id)
    ).scalar_one_or_none()
    return _view(request, owner, reviewed_by_email=reviewer)


def proof_key(db: Session, public_id: str) -> str:
    """The storage key of the file an owner uploaded to justify the change.

    Reachable only from here, so the key cannot travel in a response body and
    be turned into a URL. A request with no proof raises rather than returning
    an empty key: "there is no proof" and "here is the proof, it is empty" must
    not look alike to an admin about to approve a change of bank details.
    """
    request = _by_public_id(db, public_id)
    if not request.proof_path:
        raise NotFound(NO_PROOF)
    return request.proof_path
