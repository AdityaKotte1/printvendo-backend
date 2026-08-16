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
lease; one that goes quiet past its deadline is requeued by `requeue_expired`.
That is a decision made once, with a visible attempt count, rather than the old
behaviour of leaving every task claimable the entire time it was printing.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.printing.models import PrintTask, TaskState

# How long a device has to finish before its claim is considered lost.
#
# Generous on purpose: a large colour job on a slow kiosk printer genuinely
# takes minutes, and requeueing a task that is still printing is how you get the
# duplicate this module exists to prevent. The device renews the lease as it
# reports progress, so the deadline only matters when it has actually gone away.
LEASE = timedelta(minutes=15)

# A task that has failed this many times stops being retried. Without a cap, a
# document that crashes the printer is requeued forever and blocks the queue.
MAX_ATTEMPTS = 3


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

    Called on every progress report. A long job therefore never has its lease
    expire while it is genuinely printing -- only silence expires a lease.
    """
    now = now or datetime.now(UTC)
    task.lease_expires_at = now + LEASE
    db.add(task)
    return task


def requeue_expired(db: Session, *, now: datetime | None = None) -> list[PrintTask]:
    """Return tasks whose device went silent to the queue.

    The only path back to QUEUED. Nothing else may resurrect a claimed task,
    which is what keeps "claimed" meaningful.

    A task that has exhausted its attempts is failed rather than requeued: a
    document that crashes the printer would otherwise be handed out forever and
    block every job behind it.
    """
    now = now or datetime.now(UTC)

    stale = list(
        db.execute(
            select(PrintTask).where(
                PrintTask.state.in_(
                    [TaskState.SENT_TO_DEVICE, TaskState.PRINTING]
                ),
                PrintTask.lease_expires_at.is_not(None),
                PrintTask.lease_expires_at < now,
            )
        ).scalars()
    )

    for task in stale:
        if task.attempts >= MAX_ATTEMPTS:
            task.state = TaskState.FAILED
            task.error_code = "LEASE_EXPIRED"
            task.error_message = (
                f"The kiosk stopped responding {task.attempts} times while "
                "printing this. It has not been charged for again."
            )
            task.finished_at = now
        else:
            task.state = TaskState.QUEUED
            task.claimed_at = None
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
