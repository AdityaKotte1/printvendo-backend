"""Enrolling a kiosk's machine, and hearing that it is still there.

The backend being replaced spread its device surface across two routers
(`printers.py` and `pi.py`) and identified the caller by a printer id in the
URL. Here there is one prefix, and the device's token *is* its kiosk -- no
request names one.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import CurrentDevice, get_db
from app.api.schemas import (
    DeviceHeartbeatRequest,
    DeviceHeartbeatResponse,
    DeviceRegisterRequest,
    DeviceRegisterResponse,
)
from app.core.errors import BadRequest
from app.modules.kiosks import (
    DeviceStatus,
    record_heartbeat,
    register_device,
    sheets_remaining,
)
from app.modules.kiosks import repository as kiosk_repo
from app.modules.printing import queue_depth

router = APIRouter(prefix="/v1/device", tags=["device"])


@router.post("/register", response_model=DeviceRegisterResponse)
def register(
    payload: DeviceRegisterRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DeviceRegisterResponse:
    """Spend an enrolment code and receive a device token.

    Public by necessity: a machine being installed has no token yet. The code is
    what authorises it, and it is single-use, short-lived, and issued for one
    kiosk by someone who already had the right to touch that kiosk.
    """
    issued = register_device(
        db,
        payload.enrolment_code,
        device_key=payload.device_key,
        agent_version=payload.agent_version,
        capabilities=payload.capabilities,
    )
    kiosk = kiosk_repo.kiosk_of_device(db, issued.device)

    return DeviceRegisterResponse(
        device_key=issued.device.device_key,
        token=issued.token,
        kiosk_id=kiosk.public_id,
        kiosk_name=kiosk.name,
    )


@router.post("/heartbeat", response_model=DeviceHeartbeatResponse)
def heartbeat(
    payload: DeviceHeartbeatRequest,
    device: CurrentDevice,
    db: Annotated[Session, Depends(get_db)],
) -> DeviceHeartbeatResponse:
    """I am alive, and here is what I am running.

    Answers with what the agent needs to decide whether to ask for work: how
    much is queued and how much paper is left. One round trip rather than three.
    """
    record_heartbeat(
        db,
        device,
        agent_version=payload.agent_version,
        status=_status_from(payload.status),
    )
    kiosk = kiosk_repo.kiosk_of_device(db, device)

    return DeviceHeartbeatResponse(
        kiosk_id=kiosk.public_id,
        kiosk_name=kiosk.name,
        queue_depth=queue_depth(db, kiosk_id=kiosk.id),
        sheets_remaining=sheets_remaining(db, kiosk),
    )


def _status_from(raw: str | None) -> DeviceStatus:
    """A status the device claims, or ONLINE by default.

    An unrecognised value is refused rather than coerced. The old backend stored
    a free-form string assigned straight from the request body, and a typo put a
    kiosk into a state nothing recognised.
    """
    if raw is None:
        return DeviceStatus.ONLINE
    try:
        return DeviceStatus(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(s.value for s in DeviceStatus)
        raise BadRequest(f"{raw!r} is not a device status. Use one of: {allowed}.") from exc
