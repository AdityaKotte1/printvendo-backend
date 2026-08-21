"""Reviewing an owner's request to change where their money is paid.

The other half of `/v1/owner/payment-config/change-request`, which shipped
without it: an owner could ask, `payments.configs.review_change` could decide,
and nothing could call it. An owner whose bank account had changed submitted a
request that no one in the system was able to approve.

Two things here are the control rather than the paperwork:

* **The proof is bytes from an authenticated route.** Never a URL. The old admin
  dashboard built `API_BASE + '/storage/...'` and hung an `onerror` handler off
  the image, so a proof that failed to load looked exactly like a proof nobody
  had uploaded -- and changes of bank details were approved unseen. A route that
  returns the file to a signed-in admin, or a 404 saying there is no file, has
  no third outcome.
* **The decision is admin-only.** `require_role(Role.ADMIN)` is what makes the
  approval step worth having; without it, an account takeover approves its own
  request and redirects every rupee the owner's kiosks collect.
"""

from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_document_store, require_role
from app.api.schemas import ChangeRequestResponse, ReviewChangeRequest
from app.core.errors import NotFound
from app.modules.identity import User
from app.modules.identity.roles import Role
from app.modules.ops import audit
from app.modules.payments import (
    ChangeRequestView,
    pending_change_requests,
    proof_key,
    review_change_by_id,
)
from app.modules.printing import DocumentStore

router = APIRouter(prefix="/v1/admin/payment-config", tags=["admin"])

CurrentAdmin = Annotated[User, Depends(require_role(Role.ADMIN))]

PROOF_MISSING = (
    "That proof file is missing from storage. Do not approve this request until "
    "the owner has uploaded it again."
)

# Types an admin may view in the browser tab. An allowlist rather than whatever
# the upload claimed: an SVG or an HTML file served inline from the API's own
# origin is stored XSS, and the uploader is the person the review exists to be
# suspicious of. Anything not named here is downloaded as bytes instead.
INLINE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


def _as_response(view: ChangeRequestView) -> ChangeRequestResponse:
    return ChangeRequestResponse(
        id=view.public_id,
        owner_id=view.owner_public_id,
        owner_email=view.owner_email,
        reason=view.reason,
        has_proof=view.has_proof,
        status=view.status.value,
        created_at=view.created_at,
        reviewed_at=view.reviewed_at,
        review_note=view.review_note,
    )


@router.get("/change-requests", response_model=list[ChangeRequestResponse])
def review_queue(
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> list[ChangeRequestResponse]:
    """Everything waiting on a decision, oldest first.

    Oldest first because this is a worklist somebody works through, and the
    owner at the top has been unable to collect into the right account for the
    longest.
    """
    return [_as_response(view) for view in pending_change_requests(db)]


@router.get("/change-requests/{request_id}/proof")
def change_request_proof(
    request_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
) -> Response:
    """The file the owner uploaded to show the new account is theirs.

    Read and returned here rather than pointed at, so that "the admin could not
    see the proof" is a status code and not a broken image.
    """
    key = proof_key(db, request_id)

    try:
        data = store.read(key)
    except FileNotFoundError as exc:
        # The row says there is a file and there is not. Said plainly, because
        # the wrong response to this is to approve anyway.
        raise NotFound(PROOF_MISSING) from exc

    suffix = PurePosixPath(key).suffix.lower()
    media_type = INLINE_TYPES.get(suffix)
    disposition = "inline" if media_type else "attachment"

    return Response(
        content=data,
        media_type=media_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{request_id}{suffix}"',
            # Belt and braces with the allowlist above: no sniffing an
            # octet-stream back into something the browser will execute.
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/change-requests/{request_id}/review", response_model=ChangeRequestResponse)
def review(
    request_id: str,
    body: ReviewChangeRequest,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> ChangeRequestResponse:
    """Approve or reject. An approval authorises exactly one change.

    Consuming it is `set_keys`' job, not this route's -- the approval is spent
    when the owner actually uses it, so an admin who approves and an owner who
    never follows through leaves the keys where they are.

    Audited against the **owner**, not against the request, so that everything
    that has ever happened to that account's payment configuration -- their own
    `change.requested`, this decision, and the eventual `keys.set` -- comes back
    from one query on one entity id.
    """
    view = review_change_by_id(
        db,
        request_id,
        approve=body.approve,
        reviewer_user_id=admin.id,
        note=body.note,
    )

    audit.record(
        db,
        action="payment_config.change.reviewed",
        entity_type="user",
        entity_id=view.owner_public_id,
        actor_user_id=admin.id,
        after={"request_id": view.public_id, "status": view.status.value},
        note=body.note,
    )
    return _as_response(view)
