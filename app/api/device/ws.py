"""The socket a kiosk's Pi holds open.

A job created on any worker reaches the machine in the shop within a moment
rather than at the next poll. That is the whole benefit; the polling path stays
exactly as it was and remains the fallback whenever this is unavailable.

**The socket carries a wake, never work.** "Something is queued" goes down the
wire; the device then claims over `POST /v1/device/tasks/next`, which is a
single `FOR UPDATE SKIP LOCKED` statement. Sending the task itself would be a
second implementation of claiming, and a reconnect overlapping a publish could
hand one job to two devices -- the failure the atomic claim exists to make
unreachable.

**The token is the kiosk**, as on every other device route. Nothing the socket
receives names a kiosk, so there is no id for a handler to trust: one shop's Pi
cannot subscribe to another's work however it is addressed.

The connection is authenticated *before* it is accepted. A socket accepted first
and checked afterwards is one an unauthenticated client can hold open, and a few
thousand of them is a denial of service that costs nothing to mount.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.api.deps import get_bus, get_db
from app.core.bus import Bus
from app.core.errors import AppError
from app.modules.kiosks import authenticate_device

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/device", tags=["device"])

# Close codes. 1008 is "policy violation", which is what the WebSocket protocol
# offers for "you may not do this" -- there is no 401 to send once a handshake
# has begun.
POLICY_VIOLATION = 1008


@router.websocket("/ws")
async def device_socket(
    websocket: WebSocket,
    db: Annotated[Session, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
) -> None:
    """Hold a connection for one kiosk and tell it when there is work.

    The greeting matters: a TCP connection that was never authenticated looks
    identical from the client's side until something arrives, so an agent that
    has seen `ready` knows its socket is live rather than merely open.
    """
    try:
        device = authenticate_device(db, websocket.headers.get("X-Device-Token"))
    except AppError:
        # Refused before accepting, so an unauthenticated client never holds a
        # socket at all.
        await websocket.close(code=POLICY_VIOLATION)
        return

    kiosk_id = device.kiosk_id

    await websocket.accept()
    await websocket.send_json({"type": "ready"})

    try:
        async with bus.wakeups(kiosk_id) as stream:
            async for _ in stream:
                # The message body is deliberately ignored. A wake means "ask
                # again", and giving it a payload later must not change that:
                # whatever it said, the claim is still the authority on what
                # this device may print.
                await websocket.send_json({"type": "wake"})
    except WebSocketDisconnect:
        # The ordinary end of every socket: a shop's power goes off, the Pi
        # reboots, a network moves. Not worth a log line each time.
        pass
    except Exception:  # noqa: BLE001
        # Anything else -- Redis going away mid-subscription, most likely. The
        # device falls back to polling the moment the socket drops, so this
        # degrades rather than fails.
        logger.warning(
            "device socket for kiosk %s ended unexpectedly", kiosk_id, exc_info=True
        )
        try:
            await websocket.close(code=POLICY_VIOLATION)
        except RuntimeError:
            # Already closed from the other end.
            pass
