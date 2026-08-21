"""The one Razorpay webhook.

The backend being replaced had three endpoints, one per purpose, each with its
own signature check and its own idea of what "already handled" meant. Here there
is one handler and one path shape; what a payment was *for* comes from the
`kind` column written when its checkout was opened, not from which URL the
delivery happened to hit.

There are two path forms because owners register their own webhook against their
own Razorpay account and therefore have a signing secret of their own. The URL
names the account, and the URL is what selects the secret -- chosen before a byte
of the body is parsed, so nothing an attacker sends can influence which key
their signature is verified against.

The endpoint answers 200 to everything it can read, and that is deliberate:
Razorpay redelivers until it gets a success, so an already-handled event that
raised would be redelivered forever. Only an unverified signature is a refusal.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.deps import (
    get_db,
    get_refund_sink,
    get_secret_box,
    get_settings_from_app,
    get_settlement,
)
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.errors import NotFound
from app.modules.identity import repository as identity_repo
from app.modules.payments import (
    RefundSink,
    Settlement,
    handle_webhook,
    webhook_secret_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

UNKNOWN_ACCOUNT = "No such webhook endpoint."


def _deliver(
    db: Session,
    *,
    body: bytes,
    signature: str | None,
    collecting_user_id: int | None,
    settings: Settings,
    box: SecretBox,
    settlement: Settlement,
    refund_sink: RefundSink,
) -> dict[str, str | bool]:
    secret = webhook_secret_for(
        db,
        collecting_user_id=collecting_user_id,
        box=box,
        platform_webhook_secret=settings.RAZORPAY_WEBHOOK_SECRET,
    )

    outcome = handle_webhook(
        db,
        body=body,
        signature=signature,
        secret=secret,
        settlement=settlement,
        refund_sink=refund_sink,
        collecting_user_id=collecting_user_id,
    )

    logger.info("razorpay webhook %s: %s", outcome.event, outcome.detail)
    return {"event": outcome.event, "handled": outcome.handled, "detail": outcome.detail}


@router.post("/razorpay")
async def platform_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    settlement: Annotated[Settlement, Depends(get_settlement)],
    refund_sink: Annotated[RefundSink, Depends(get_refund_sink)],
    x_razorpay_signature: Annotated[str | None, Header()] = None,
) -> dict[str, str | bool]:
    """Deliveries for the platform's own Razorpay account.

    The **raw** body is what gets verified. Parsing first and re-serialising
    would let anyone change an amount and keep the signature, because the bytes
    that were signed and the bytes that were checked would no longer be the
    same.
    """
    return _deliver(
        db,
        body=await request.body(),
        signature=x_razorpay_signature,
        collecting_user_id=None,
        settings=settings,
        box=box,
        settlement=settlement,
        refund_sink=refund_sink,
    )


@router.post("/razorpay/{owner_id}")
async def owner_webhook(
    owner_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    box: Annotated[SecretBox, Depends(get_secret_box)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    settlement: Annotated[Settlement, Depends(get_settlement)],
    refund_sink: Annotated[RefundSink, Depends(get_refund_sink)],
    x_razorpay_signature: Annotated[str | None, Header()] = None,
) -> dict[str, str | bool]:
    """Deliveries for one owner's account, on a URL that names them.

    The owner id in the path selects which secret verifies the signature, and
    `handle_webhook` then refuses any event about a payment a *different*
    account collected. Both checks matter: an owner legitimately holds a secret
    that verifies against this endpoint, so without the second one they could
    settle a competitor's takings.
    """
    # `get_by_public_id` parses the id itself and returns None on a malformed
    # one, so an unparseable id and an unknown owner give the same 404 -- this
    # URL is public, and telling a prober which of the two it hit would let it
    # enumerate owner ids.
    owner = identity_repo.get_by_public_id(db, owner_id)
    if owner is None:
        raise NotFound(UNKNOWN_ACCOUNT)

    return _deliver(
        db,
        body=await request.body(),
        signature=x_razorpay_signature,
        collecting_user_id=owner.id,
        settings=settings,
        box=box,
        settlement=settlement,
        refund_sink=refund_sink,
    )
