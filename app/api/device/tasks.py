"""Handing work to a kiosk's machine, and hearing what happened to it.

Three routes replace the old backend's `GET /pi/{printer_id}/jobs/next`, whose
two defects are both closed here:

* it returned a job **without claiming it**, so the agent's main loop and its
  prefetch worker could receive the same one and both print it. `/tasks/next`
  claims atomically -- two callers cannot receive the same task.
* its file endpoint was device-token-wide: any registered Pi could fetch any
  job's file by id. Here a device may only read a file for a task at its own
  kiosk, and only while it holds the claim.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import CurrentDevice, KioskPaperLedger, get_db, get_document_store
from app.api.schemas import DeviceTaskResponse, DeviceTaskStatusRequest
from app.core.errors import BadRequest, Conflict, NotFound
from app.modules.kiosks import repository as kiosk_repo
from app.modules.printing import (
    DocumentStore,
    TaskState,
    claim_next_task,
    printable_key,
    report_blocked,
    report_failed,
    report_printed,
    start_printing,
)
from app.modules.printing import repository as printing_repo

router = APIRouter(prefix="/v1/device/tasks", tags=["device"])

# States a device may report. `printed`, `failed` and `blocked` finish the
# device's involvement; `printing` says it has started. Anything else -- a
# device claiming `queued`, say -- is refused rather than mapped to something
# nearby, because the queue is not the device's to rewrite.
REPORTABLE = {
    TaskState.PRINTING,
    TaskState.PRINTED,
    TaskState.FAILED,
    TaskState.BLOCKED,
}

NO_LONGER_HELD = "This kiosk is not currently printing that job."


def _as_response(task, document) -> DeviceTaskResponse:
    return DeviceTaskResponse(
        task_id=task.public_id,
        document_id=document.public_id,
        filename=document.original_filename,
        file_url=f"{router.prefix}/{task.public_id}/file",
        page_count=document.page_count,
        copies=task.copies,
        duplex=task.duplex,
        colour=task.colour,
        page_range=task.page_range,
        expected_sheets=task.predicted_sheets,
        lease_expires_at=task.lease_expires_at,
    )


@router.post("/next", response_model=DeviceTaskResponse | None)
def next_task(
    device: CurrentDevice,
    db: Annotated[Session, Depends(get_db)],
) -> DeviceTaskResponse | None:
    """Claim the next job for this kiosk, or answer null.

    The claim is the same statement as the read (`FOR UPDATE SKIP LOCKED`), so
    however many agents, threads or prefetch workers ask at once, no two of them
    receive the same task.
    """
    task = claim_next_task(db, kiosk_id=device.kiosk_id)
    if task is None:
        return None

    return _as_response(task, printing_repo.document_of(db, task))


@router.get("/{task_id}/file")
def task_file(
    task_id: str,
    device: CurrentDevice,
    db: Annotated[Session, Depends(get_db)],
    store: Annotated[DocumentStore, Depends(get_document_store)],
) -> Response:
    """The PDF to print.

    Bound three ways: the task must belong to this device's kiosk, this device
    must currently hold it, and the file served is the whole document -- never a
    page-trimmed copy, because the range is applied at the printer and trimming
    here too would apply it twice.
    """
    task = printing_repo.task_for_kiosk(
        db, kiosk_id=device.kiosk_id, public_id=task_id
    )
    if task.state not in (TaskState.SENT_TO_DEVICE, TaskState.PRINTING):
        raise Conflict(NO_LONGER_HELD)

    document = printing_repo.document_of(db, task)
    key = printable_key(document)

    try:
        data = store.read(key)
    except FileNotFoundError as exc:
        # The row says there is a file and there is not. Retention removing it
        # underneath a live task should be impossible, so this is worth a 404
        # the agent can report rather than a 500 it cannot.
        raise NotFound(
            "That document's file is missing. Please ask the student to upload it again."
        ) from exc

    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{task.public_id}.pdf"'},
    )


@router.post("/{task_id}/status", response_model=DeviceTaskResponse)
def report_status(
    task_id: str,
    payload: DeviceTaskStatusRequest,
    device: CurrentDevice,
    db: Annotated[Session, Depends(get_db)],
) -> DeviceTaskResponse:
    """What happened to a job.

    Paper is deducted here, from what the printer says it used. The ledger is
    built around this device's kiosk, so a report can only ever debit the tray
    that printed.
    """
    task = printing_repo.task_for_kiosk(
        db, kiosk_id=device.kiosk_id, public_id=task_id
    )
    state = _reported_state(payload.state)
    kiosk = kiosk_repo.kiosk_of_device(db, device)
    ledger = KioskPaperLedger(kiosk)

    if state is TaskState.PRINTING:
        start_printing(db, task)
    elif state is TaskState.PRINTED:
        report_printed(db, task, ledger, sheets_used=payload.sheets_used)
    elif state is TaskState.FAILED:
        report_failed(
            db,
            task,
            ledger,
            sheets_used=payload.sheets_used,
            error_code=payload.error_code,
            error_message=payload.error_message,
        )
    else:
        report_blocked(
            db,
            task,
            reason=payload.error_code or "BLOCKED",
            message=payload.error_message,
        )

    return _as_response(task, printing_repo.document_of(db, task))


def _reported_state(raw: str) -> TaskState:
    try:
        state = TaskState(raw.strip().lower())
    except ValueError as exc:
        raise BadRequest(_unreportable(raw)) from exc

    if state not in REPORTABLE:
        raise BadRequest(_unreportable(raw))
    return state


def _unreportable(raw: str) -> str:
    allowed = ", ".join(sorted(s.value for s in REPORTABLE))
    return f"{raw!r} is not something a kiosk can report. Use one of: {allowed}."
