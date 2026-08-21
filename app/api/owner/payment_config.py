"""Where an owner's takings go, and how that is allowed to change.

Nothing here ever returns a secret. `PaymentConfigView` has no field for one, so
the question "did I remember to strip it?" cannot be answered wrongly. The
backend being replaced stored `razorpay_key_secret` as a plain String while its
own comment claimed the value was encrypted, and a database dump therefore
handed over every owner's live payment credentials.

**Keys are set once and then only replaced through an approved request.** That
is an anti-fraud control, not bureaucracy. Owners are paid directly, so there is
no settlement run where a discrepancy would surface: anyone who takes over an
owner's account could silently redirect every student payment at every one of
their kiosks, and the owner would find out when they wondered why takings
stopped. The approval step puts a person between an account takeover and the
money.

Until an owner completes this, `kiosk_payment_gate` returns CLOSED for their
SOLD and SAAS kiosks, which cannot then go LIVE. So this router is the thing
standing between a shop being sold and a shop being able to print.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    get_document_store,
    get_secret_box,
    get_settings_from_app,
    require_role,
)
from app.api.schemas import (
    PaymentConfigResponse,
    SetPaymentKeysRequest,
    SetWebhookSecretRequest,
    WebhookEndpointResponse,
)
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import BadRequest
from app.modules.identity import User
from app.modules.identity.roles import Role
from app.modules.ops import audit
from app.modules.payments import (
    PaymentConfigView,
    request_change,
    set_keys,
    set_webhook_secret,
    view_config,
)
from app.modules.printing import DocumentStore
from app.modules.printing.storage import StorageArea

router = APIRouter(prefix="/v1/owner/payment-config", tags=["owner"])

CurrentOwner = Annotated[User, Depends(require_role(Role.OWNER))]

PROOF_TOO_LARGE = "That file is too large. Proof of account ownership must be under 8 MB."
MAX_PROOF_BYTES = 8 * 1024 * 1024


def _as_response(view: PaymentConfigView) -> PaymentConfigResponse:
    return PaymentConfigResponse(
        is_configured=view.is_configured,
        key_id_masked=view.key_id_masked,
        configured_at=view.configured_at,
        can_update=view.can_update,
    )


@router.get("", response_model=PaymentConfigResponse)
def my_payment_config(
    owner: CurrentOwner,
    db: Annotated[Session, Depends(get_db)],
) -> PaymentConfigResponse:
    """What is configured, masked.

    `can_update` is the whole state machine as one boolean: true when nothing is
    set yet, or when an admin has approved a change that has not been used. The
    owner app renders a form or a "request a change" button from it, so the two
    cannot disagree about whether an edit is currently possible.
    """
    return _as_response(view_config(db, owner.id))


@router.put("/keys", response_model=PaymentConfigResponse)
def update_keys(
    body: SetPaymentKeysRequest,
    owner: CurrentOwner,
    db: Annotated[Session, Depends(get_db)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
) -> PaymentConfigResponse:
    """Set the Razorpay keys money will be collected into.

    Refused when keys are already set and no approved change request is
    waiting -- `set_keys` decides that, and consumes the approval, so one
    approval authorises exactly one change.

    The audit entry records that keys changed and which key *id* they moved to.
    The secret is scrubbed on the way in by `ops.audit`, so the trail can say
    what happened without becoming a second copy of the credential.
    """
    before = view_config(db, owner.id)

    set_keys(
        db,
        owner.id,
        key_id=body.key_id,
        key_secret=body.key_secret,
        box=box,
    )

    after = view_config(db, owner.id)
    audit.record(
        db,
        action="payment_config.keys.set",
        entity_type="user",
        entity_id=owner.public_id,
        actor_user_id=owner.id,
        before={"is_configured": before.is_configured, "key_id": before.key_id_masked},
        after={"is_configured": after.is_configured, "key_id": after.key_id_masked},
    )
    return _as_response(after)


@router.put("/webhook-secret", response_model=PaymentConfigResponse)
def update_webhook_secret(
    body: SetWebhookSecretRequest,
    owner: CurrentOwner,
    db: Annotated[Session, Depends(get_db)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
) -> PaymentConfigResponse:
    """Set the signing secret for this owner's own Razorpay webhook.

    Razorpay treats this as a separate value from the API key secret -- it is
    set per webhook in their dashboard, not per key -- so it is stored and
    changed separately here too. Without it, deliveries to this owner's endpoint
    fail closed: `webhook_secret_for` returns "" and an empty secret verifies
    nothing, which is correct rather than exceptional for an owner who has not
    set a webhook up.
    """
    set_webhook_secret(db, owner.id, webhook_secret=body.webhook_secret, box=box)

    audit.record(
        db,
        action="payment_config.webhook_secret.set",
        entity_type="user",
        entity_id=owner.public_id,
        actor_user_id=owner.id,
    )
    return _as_response(view_config(db, owner.id))


@router.get("/webhook-endpoint", response_model=WebhookEndpointResponse)
def my_webhook_endpoint(
    owner: CurrentOwner,
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> WebhookEndpointResponse:
    """The URL this owner registers in their own Razorpay dashboard.

    Handed to them rather than left to be constructed by hand, because a typo
    here is silent: deliveries arrive at a URL naming a different account, the
    signature check refuses them, and the owner's payments simply never settle
    with no error anyone sees.
    """
    return WebhookEndpointResponse(
        url=f"{settings.PUBLIC_BASE_URL}/v1/webhooks/razorpay/{owner.public_id}",
        events=["payment.captured", "payment.failed", "refund.processed"],
    )


@router.post("/change-request", status_code=status.HTTP_202_ACCEPTED)
async def request_key_change(
    owner: CurrentOwner,
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
    reason: Annotated[str | None, Form()] = None,
    proof: Annotated[UploadFile | None, File()] = None,
) -> dict[str, str]:
    """Ask an admin to allow the keys to be replaced.

    `proof` is a file showing the new account belongs to this owner. It is
    stored under an opaque key and served only through an authenticated admin
    route -- never as a static URL. The old admin dashboard built
    `API_BASE + '/storage/...'`, which 404s silently behind an `onerror`
    handler, so admins were approving these having never seen the proof.
    """
    proof_path = None
    if proof is not None:
        data = await proof.read(MAX_PROOF_BYTES + 1)
        if len(data) > MAX_PROOF_BYTES:
            raise BadRequest(PROOF_TOO_LARGE)
        proof_path = store.save(
            StorageArea.PROOF,
            user_id=owner.id,
            filename=proof.filename,
            data=data,
        )

    change = request_change(db, owner.id, reason=reason, proof_path=proof_path)
    # Flushed so the column default is populated: `status` is a server/ORM
    # default, so reading `.status` on an unflushed row gives None.
    db.flush()

    audit.record(
        db,
        action="payment_config.change.requested",
        entity_type="user",
        entity_id=owner.public_id,
        actor_user_id=owner.id,
        note=reason,
    )
    return {"status": change.status.value}
