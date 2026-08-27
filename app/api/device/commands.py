"""What the machine in a shop asks for besides work, and what it admits to.

Two things, and they are the same subject: whether the machine is well.

The device claims commands over HTTP exactly as it claims print tasks — the
socket carries a wake and never work. And it reports being stuck over HTTP too,
because "I cannot get this out of the printer" is a fact about the shop that has
to survive the agent being restarted to fix it.

No request here names a kiosk. The token *is* the kiosk, as everywhere under
`/v1/device`.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import CurrentDevice, get_billing_check, get_db
from app.api.schemas import DeviceCommandResponse
from app.modules.kiosks import (
    BillingCheck,
    KioskDeviceCommand,
    claim_commands,
    report_command,
    report_recovered,
    report_stuck,
)
from app.modules.kiosks import repository as kiosk_repo
from app.modules.ops import AlertSeverity, alerts

router = APIRouter(prefix="/v1/device", tags=["device"])


class CommandResultRequest(BaseModel):
    succeeded: bool
    error_message: str | None = None


class PrinterHealthRequest(BaseModel):
    """Whether this machine can currently get a job out of the printer.

    One field, deliberately. A machine that reports a taxonomy of printer
    faults is a machine whose taxonomy has to agree with the server's, and the
    only decision the server makes from it is whether the shop can sell.
    """

    stuck: bool
    detail: str | None = None


def _as_command(command: KioskDeviceCommand) -> DeviceCommandResponse:
    return DeviceCommandResponse(
        id=command.public_id,
        command=command.kind.value,
        state=command.state.value,
        error_message=command.error_message,
        requested_at=command.created_at,
        sent_at=command.sent_at,
        finished_at=command.finished_at,
    )


@router.post("/commands/next", response_model=list[DeviceCommandResponse])
def next_commands(
    device: CurrentDevice,
    db: Annotated[Session, Depends(get_db)],
) -> list[DeviceCommandResponse]:
    """Everything waiting for this machine.

    A list rather than one, unlike a print task. Restarting the agent kills the
    loop that would have come back for the second command, so a machine handed
    one per pass would silently drop whatever was behind a restart.
    """
    return [_as_command(c) for c in claim_commands(db, device)]


@router.post(
    "/commands/{command_id}/result", response_model=DeviceCommandResponse
)
def report_result(
    command_id: str,
    payload: CommandResultRequest,
    device: CurrentDevice,
    db: Annotated[Session, Depends(get_db)],
) -> DeviceCommandResponse:
    """How it went.

    A `restart_agent` that succeeds is usually never reported — the process
    that would have reported it is the one that was restarted. That is why a
    command sitting at `sent` is not treated as a failure: the operator is
    looking at whether the machine came back, which the heartbeat answers.
    """
    command = report_command(
        db,
        device,
        command_id,
        succeeded=payload.succeeded,
        error_message=payload.error_message,
    )
    return _as_command(command)


@router.post("/printer-health", status_code=204)
def printer_health(
    payload: PrinterHealthRequest,
    device: CurrentDevice,
    db: Annotated[Session, Depends(get_db)],
    billing: Annotated[BillingCheck, Depends(get_billing_check)],
) -> None:
    """The printer is stuck, or it is working again.

    Stuck closes the shop: the kiosk moves to MAINTENANCE, which `is_selling`
    already excludes, so the student app stops offering it while every operator
    surface still shows it and says why. Nobody is charged for a print that was
    never going to come out.

    Working again reopens it — but only a shop this mechanism closed. An owner
    who put their own kiosk into maintenance to change a cartridge must not
    have it reopened under them because a queue happened to clear.

    The alert is raised only on the transition, and stood down when the
    condition clears, because a machine repeating itself every minute would
    otherwise fill the console with one shop.
    """
    kiosk = kiosk_repo.kiosk_of_device(db, device)
    key = f"printer-stuck:{kiosk.public_id}"

    if payload.stuck:
        if report_stuck(db, device, kiosk, billing=billing):
            alerts.raise_alert(
                db,
                dedupe_key=key,
                kind="kiosk.printer_stuck",
                severity=AlertSeverity.CRITICAL,
                summary=f"{kiosk.name} cannot finish a print job and has been closed.",
                detail={"reported": payload.detail} if payload.detail else None,
                entity_type="kiosk",
                entity_id=kiosk.public_id,
            )
        return

    if report_recovered(db, device, kiosk, billing=billing):
        alerts.resolve_by_key(db, dedupe_key=key)
