"""Accepting an invitation to work at a kiosk.

Lives under the student prefix rather than owner or refiller because the person
accepting is, at that moment, neither -- the invitation is what makes them one.
Requiring a refiller role to accept a refiller invitation would be circular.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_db
from app.modules.kiosks.staffing import accept_invite

router = APIRouter(prefix="/v1/app/staff", tags=["staff"])


class AcceptInviteRequest(BaseModel):
    token: str


class AcceptedResponse(BaseModel):
    kiosk_id: str
    role: str


@router.post("/accept-invite", response_model=AcceptedResponse)
def accept(
    payload: AcceptInviteRequest,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> AcceptedResponse:
    """Redeem an invitation addressed to the signed-in user.

    The service refuses a token issued to a different address, so forwarding
    the email does not let someone else attach themselves to the kiosk.
    """
    kiosk, role = accept_invite(db, payload.token, user=user)
    return AcceptedResponse(kiosk_id=kiosk.public_id, role=role.value)
