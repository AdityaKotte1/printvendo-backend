"""What a device reports back about the work it was given.

Every transition out of a claim goes through this module, and each one is
guarded by the state the task is actually in. That guard is what makes a
repeated status call harmless: an agent whose network timed out will retry, and
without it the tray would be debited twice for one print.

**Paper is deducted here, from what the printer says it used.** The backend
being replaced deducted its own estimate and only on `PRINTED`, so a job that
jammed after three sheets deducted zero and the counter drifted away from the
physical tray every time anything went wrong.

The deduction goes through a `PaperLedger` handed in by the caller rather than
by importing the kiosks module. Two reasons, in order of importance: printing
and kiosks stay independent contexts, and the function *cannot* be called
without saying where the paper goes -- there is no default that quietly does
nothing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.errors import BadRequest, Conflict
from app.modules.printing.claims import LEASE
from app.modules.printing.models import TERMINAL_TASK_STATES, PrintTask, TaskState
from app.modules.printing.repository import failed_tasks_at

# States a device may report from. A task it has not claimed is either a bug or
# another kiosk's work, and either way it does not get to move it.
IN_HAND = frozenset({TaskState.SENT_TO_DEVICE, TaskState.PRINTING})

ALREADY_FINISHED = "That print job has already finished."
NOT_IN_HAND = "That print job is not currently assigned to this kiosk."
NOT_FAILED = (
    "Only a print that was reported as failed can be confirmed as printed."
)


class PaperLedger(Protocol):
    """Where a finished print's paper is deducted.

    Implemented over `kiosks.paper.consume_paper` at the composition root.
    """

    def consume(
        self,
        db: Session,
        kiosk_id: int,
        *,
        predicted_sheets: int,
        actual_sheets: int | None,
        reference: str,
    ) -> None: ...


class TaskOutcome(Protocol):
    """What a print's progress means to whoever bought it.

    Printing owns the task and must not import orders, so "this order has
    moved" arrives through here, wired at the composition root -- the same seam
    as `PaperLedger`. Without it the order's own states existed and nothing
    ever set them: a student's screen said "queued" while the paper was already
    in their hand.

    It is called on **every** move, not only the last one, because the middle
    state is the one a student is standing there reading.
    """

    def task_moved(self, db: Session, task: PrintTask) -> None: ...


class NoOutcome:
    """For callers with no order behind the task. Explicit rather than a
    default argument, so nobody skips the seam without saying so."""

    def task_moved(self, db: Session, task: PrintTask) -> None:
        return None


def _require_in_hand(task: PrintTask) -> None:
    if task.state in TERMINAL_TASK_STATES:
        raise Conflict(ALREADY_FINISHED)
    if task.state not in IN_HAND:
        raise Conflict(NOT_IN_HAND)


def start_printing(
    db: Session,
    task: PrintTask,
    *,
    outcome: TaskOutcome | None = None,
    now: datetime | None = None,
) -> PrintTask:
    """The **printer** has it -- not the device, the printer.

    The agent reports this when the job reaches the head of the queue, not when
    it hands the file over: a job waiting behind somebody else's two hundred
    pages is still queued, and a student told "printing" walks to the shop to
    collect nothing.

    The order hears about it too, so the screen moves from queued to printing
    while it is happening rather than jumping straight to printed at the end.

    Extends the lease: a long colour job must not be requeued underneath the
    machine that is printing it, which would produce the duplicate this module
    exists to prevent.
    """
    _require_in_hand(task)
    now = now or datetime.now(UTC)

    task.state = TaskState.PRINTING
    task.started_at = task.started_at or now
    task.lease_expires_at = now + LEASE
    db.add(task)

    (outcome or NoOutcome()).task_moved(db, task)
    return task


def report_printed(
    db: Session,
    task: PrintTask,
    ledger: PaperLedger,
    *,
    sheets_used: int | None,
    outcome: TaskOutcome | None = None,
    now: datetime | None = None,
) -> PrintTask:
    """It came out of the printer.

    `sheets_used` is CUPS's `job-media-sheets-completed`, which is the truth.
    An agent too old to report it leaves it None and the prediction is used --
    the only case where a guess is deducted.
    """
    _require_in_hand(task)
    _reject_negative(sheets_used)
    now = now or datetime.now(UTC)

    task.state = TaskState.PRINTED
    task.actual_sheets = sheets_used
    task.finished_at = now
    task.lease_expires_at = None
    db.add(task)

    ledger.consume(
        db,
        task.kiosk_id,
        predicted_sheets=task.predicted_sheets,
        actual_sheets=sheets_used,
        reference=task.public_id,
    )

    (outcome or NoOutcome()).task_moved(db, task)
    return task


def confirm_printed(
    db: Session,
    task: PrintTask,
    ledger: PaperLedger,
    *,
    outcome: TaskOutcome | None = None,
    now: datetime | None = None,
) -> PrintTask:
    """An operator says a job the device reported FAILED did come out after all.

    It happens: somebody clears the jam, or reloads the tray, and the job the
    agent gave up on prints. Until now the task stayed FAILED for ever, the
    order behind it stayed Partly failed, and the only honest thing an operator
    could do was refund a print the student had collected.

    **Only from FAILED, and it is not `report_printed`.** That function refuses
    a finished task on purpose -- a device reporting late must not resurrect
    one -- and this is a person deciding, recorded by the route that calls it.
    Anything else is refused: a job still on the machine would have its paper
    taken now and again when the device reports it.

    **The tray loses what the failure did not take.** `report_failed` deducted
    only what the device reported, and nothing when it said nothing, so a job
    that did print in full leaves the counter short by the rest. Taken here,
    once: `consume_paper` adds whatever it is given and dedupes nothing, so the
    task leaving FAILED is what stops a second click emptying the tray twice.

    **The failure reason is cleared.** A printed job that still says "tray
    jammed" reads as broken to whoever looks at it next. Who confirmed it, and
    what the device had said, belong in the audit entry the route writes.
    """
    if task.state is not TaskState.FAILED:
        raise Conflict(NOT_FAILED)
    now = now or datetime.now(UTC)

    already = task.actual_sheets or 0
    rest = max(0, task.predicted_sheets - already)

    task.state = TaskState.PRINTED
    task.actual_sheets = max(already, task.predicted_sheets)
    task.error_code = None
    task.error_message = None
    task.finished_at = now
    task.lease_expires_at = None
    db.add(task)

    if rest:
        ledger.consume(
            db,
            task.kiosk_id,
            # The remainder is both the prediction and the figure: nothing is
            # being estimated here, so the refill log should not read as a
            # discrepancy.
            predicted_sheets=rest,
            actual_sheets=rest,
            reference=f"{task.public_id} (confirmed by an operator)",
        )

    (outcome or NoOutcome()).task_moved(db, task)
    return task


@dataclass(frozen=True)
class ConfirmedPrint:
    """One print an operator said came out, and why it had been called failed.

    The reason travels out because `confirm_printed` clears it from the task --
    a print that came out should not read as broken to whoever looks next --
    and the caller's audit entry is where it belongs from now on.
    """

    task_id: str
    error_code: str | None
    error_message: str | None


def confirm_failed_prints(
    db: Session,
    *,
    document_ids: list[int],
    kiosk_id: int,
    since: datetime,
    ledger: PaperLedger,
) -> list[ConfirmedPrint]:
    """Confirm every failed print among one order's prints.

    Printing owns the tasks, so orders asks for this rather than being handed
    `PrintTask`s to flip. What counts as one order's prints is
    `repository._one_orders`, the same bound the order's state is derived
    through -- two definitions would let a confirm flip one set of prints while
    the order re-derived from another.
    """
    confirmed: list[ConfirmedPrint] = []
    for task in failed_tasks_at(
        db, document_ids=document_ids, kiosk_id=kiosk_id, since=since
    ):
        confirmed.append(
            ConfirmedPrint(
                task_id=task.public_id,
                error_code=task.error_code,
                error_message=task.error_message,
            )
        )
        confirm_printed(db, task, ledger)
    return confirmed


def report_failed(
    db: Session,
    task: PrintTask,
    ledger: PaperLedger,
    *,
    sheets_used: int | None,
    error_code: str | None = None,
    error_message: str | None = None,
    outcome: TaskOutcome | None = None,
    now: datetime | None = None,
) -> PrintTask:
    """It did not come out, or only part of it did.

    Whatever the device says it managed is still gone from the tray. When it
    says nothing, **zero** is recorded rather than the prediction: a job that
    may never have reached the paper path must not empty a tray on paper. The
    prediction is passed alongside, so the refill log shows the discrepancy
    instead of hiding it.
    """
    _require_in_hand(task)
    _reject_negative(sheets_used)
    now = now or datetime.now(UTC)

    consumed = sheets_used if sheets_used is not None else 0

    task.state = TaskState.FAILED
    task.actual_sheets = sheets_used
    task.error_code = error_code
    task.error_message = error_message
    task.finished_at = now
    task.lease_expires_at = None
    db.add(task)

    ledger.consume(
        db,
        task.kiosk_id,
        predicted_sheets=task.predicted_sheets,
        actual_sheets=consumed,
        reference=task.public_id,
    )

    # A failure finishes the task as surely as a success does, and the order
    # behind it has to hear about both -- half an order printed is a real
    # outcome the student is owed the difference on.
    (outcome or NoOutcome()).task_moved(db, task)
    return task


def report_blocked(
    db: Session,
    task: PrintTask,
    *,
    reason: str,
    message: str | None = None,
    now: datetime | None = None,
) -> PrintTask:
    """The device will not attempt it: no paper, offline printer, wrong media.

    Nothing was printed, so nothing is deducted, and the task is deliberately
    not failed -- it runs once the tray is filled. The lease is released because
    nobody is working on it; leaving one would have the sweeper requeue it
    behind the operator's back.
    """
    _require_in_hand(task)
    now = now or datetime.now(UTC)

    task.state = TaskState.BLOCKED
    task.error_code = reason
    task.error_message = message
    task.claimed_at = None
    task.lease_expires_at = None
    db.add(task)
    return task


def _reject_negative(sheets: int | None) -> None:
    """A device reporting minus three sheets would credit the tray."""
    if sheets is not None and sheets < 0:
        raise BadRequest("A print cannot use a negative number of sheets.")
