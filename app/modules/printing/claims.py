"""Handing a print task to a device, exactly once.

The backend being replaced returned the oldest queued job and left its status
untouched (`routers/pi.py:372`). The agent asks for work from two places -- its
main loop and a prefetch worker -- so both could receive the same job and both
print it. An agent that died after printing but before reporting left the task
queued, and reprinted it on restart.

The fix is that claiming and handing out are the same operation:

    UPDATE print_tasks SET state = 'sent_to_device', ...
    WHERE id = (
        SELECT id FROM print_tasks
        WHERE kiosk_id = :kiosk AND state = 'queued'
        ORDER BY position, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING ...

`FOR UPDATE SKIP LOCKED` is the standard Postgres work-queue pattern. A second
concurrent caller does not block and does not error -- it skips the locked row
and takes the next one, or gets nothing. Two requests physically cannot receive
the same task, however many agents, threads or prefetchers are asking.

Crash recovery is deliberately a *separate* mechanism. A claimed task carries a
lease, renewed by the device's heartbeat while it holds the task; one whose
device goes quiet past the deadline is **failed by `fail_expired`, never handed
out again**. It used to be requeued, and a Windows kiosk whose printer was out
of paper held a job in its spooler past the lease, was handed the same job
again, spooled a second copy and then a third -- and when paper went in, every
copy came out. Only a person can tell whether a lost job printed, so its order
shows it failed and they refund it or mark it printed.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.printing.models import PrintTask, TaskState

# How long a device has to finish before its claim is considered lost.
#
# Generous on purpose: a large colour job on a slow kiosk printer genuinely
# takes minutes. The device renews the lease with a heartbeat that names the
# task it holds, so the deadline only matters when it has actually gone quiet.
LEASE = timedelta(minutes=15)

# States in which a device holds a task. Anything else is either waiting to be
# claimed or finished, and neither has a lease to renew or to lose.
_HELD = (TaskState.SENT_TO_DEVICE, TaskState.PRINTING)


def claim_next_task(
    db: Session, *, kiosk_id: int, now: datetime | None = None
) -> PrintTask | None:
    """Take the next queued task for this kiosk, or None if there is nothing.

    Atomic: the row is claimed in the same statement that selects it.
    """
    now = now or datetime.now(UTC)

    inner = (
        select(PrintTask.id)
        .where(PrintTask.kiosk_id == kiosk_id, PrintTask.state == TaskState.QUEUED)
        .order_by(PrintTask.position, PrintTask.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    stmt = (
        update(PrintTask)
        .where(PrintTask.id == inner)
        .values(
            state=TaskState.SENT_TO_DEVICE,
            claimed_at=now,
            lease_expires_at=now + LEASE,
            attempts=PrintTask.attempts + 1,
        )
        .returning(PrintTask.id)
    )

    claimed_id = db.execute(stmt).scalar_one_or_none()
    if claimed_id is None:
        return None

    task = db.get(PrintTask, claimed_id)

    # The UPDATE ran as SQL, so a copy of this row already in the session is
    # stale. SQLAlchemy synchronises *some* of it back -- `state` and `attempts`
    # arrive updated -- while `claimed_at` and `lease_expires_at` do not, which
    # is worse than nothing synchronising at all: the object looks half-claimed
    # and a caller reading its lease deadline gets None. Refresh so what is
    # returned is what is in the database.
    db.refresh(task)
    return task


def renew_lease(
    db: Session, task: PrintTask, *, now: datetime | None = None
) -> PrintTask:
    """Push back the deadline because the device is still working.

    Reached through `renew_held_lease` whenever the device's heartbeat names the
    task. A long job therefore never has its lease expire while its machine is
    still answering -- only silence expires a lease.
    """
    now = now or datetime.now(UTC)
    task.lease_expires_at = now + LEASE
    db.add(task)
    return task


def renew_held_lease(
    db: Session, *, kiosk_id: int, task_public_id: str, now: datetime | None = None
) -> bool:
    """Renew the lease of the task a device says it is holding. True if renewed.

    Only a task this kiosk holds. Anything else -- another kiosk's, a finished
    one, an id that means nothing -- is ignored rather than refused, because the
    caller is a heartbeat, and a heartbeat that fails makes a working shop look
    offline.
    """
    task = db.execute(
        select(PrintTask).where(
            PrintTask.public_id == task_public_id,
            PrintTask.kiosk_id == kiosk_id,
            PrintTask.state.in_(_HELD),
        )
    ).scalar_one_or_none()
    if task is None:
        return False
    renew_lease(db, task, now=now)
    return True


def fail_expired(db: Session, *, now: datetime | None = None) -> list[PrintTask]:
    """Fail the tasks whose device went quiet past their lease.

    **Never back to the queue.** The device may have printed it: a job waiting
    in a Windows spooler behind an empty tray is still in that spooler when a
    second copy is sent, and both come out when paper goes in. Nothing on the
    server can tell a job that never started from one sitting in a spooler, so
    this decides nothing about the paper -- it records that nobody knows, and
    the order derives PARTIALLY_FAILED for a person to refund or mark printed.
    """
    now = now or datetime.now(UTC)

    stale = list(
        db.execute(
            select(PrintTask).where(
                PrintTask.state.in_(_HELD),
                PrintTask.lease_expires_at.is_not(None),
                PrintTask.lease_expires_at < now,
            )
        ).scalars()
    )

    for task in stale:
        task.state = TaskState.FAILED
        task.error_code = "LEASE_EXPIRED"
        task.error_message = (
            "The kiosk stopped answering while it had this job. It was not sent "
            "again, because it may already have printed."
        )
        task.finished_at = now
        task.lease_expires_at = None
        db.add(task)

    return stale

def queue_depth(db: Session, *, kiosk_id: int) -> int:
    """How many tasks are waiting at this kiosk.

    Counts only QUEUED. A claimed task is somebody's problem already, and
    including it would make the number a student sees on the app jump around as
    devices pick work up.
    """
    stmt = select(PrintTask.id).where(
        PrintTask.kiosk_id == kiosk_id, PrintTask.state == TaskState.QUEUED
    )
    return len(list(db.execute(stmt).scalars()))
