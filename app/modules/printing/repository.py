"""Reads against the printing tables, always bound to something.

There is no "get task by id". Every lookup here names the kiosk or the user it
must belong to, so a caller cannot fetch a row it has no relationship with --
the same rule the kiosks repository enforces with `Scope`, applied to the two
identities that reach these tables: a device (bound to one kiosk) and a student
(bound to their own documents).

A row outside the caller's reach is `NotFound`, never `Forbidden`, and the
message is byte-identical to one that never existed. A 403 would confirm that
another kiosk holds a task with that id.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.printing.models import Document, PrintTask, TaskState

NO_SUCH_TASK = "That print job does not exist."
NO_SUCH_DOCUMENT = "That document does not exist."


def task_for_kiosk(db: Session, *, kiosk_id: int, public_id: str) -> PrintTask:
    """One kiosk's print task. Another kiosk's is indistinguishable from none."""
    try:
        parse_id(public_id, IdPrefix.PRINT_TASK)
    except InvalidId as exc:
        # A document id passed where a task id belongs is a caller bug, not a
        # missing row -- but saying so would confirm the id's shape is right.
        raise NotFound(NO_SUCH_TASK) from exc

    task = db.execute(
        select(PrintTask).where(
            PrintTask.public_id == public_id, PrintTask.kiosk_id == kiosk_id
        )
    ).scalar_one_or_none()
    if task is None:
        raise NotFound(NO_SUCH_TASK)
    return task


def document_of(db: Session, task: PrintTask) -> Document:
    """The file a task prints.

    Takes the task rather than an id: the caller has already been through a
    bound read to hold one, so this cannot reach outside it.
    """
    document = db.get(Document, task.document_id)
    if document is None:
        raise NotFound(NO_SUCH_DOCUMENT)
    return document


def document_for_user(db: Session, *, user_id: int, public_id: str) -> Document:
    """A student's own document. Someone else's is indistinguishable from none."""
    try:
        parse_id(public_id, IdPrefix.DOCUMENT)
    except InvalidId as exc:
        raise NotFound(NO_SUCH_DOCUMENT) from exc

    document = db.execute(
        select(Document).where(
            Document.public_id == public_id, Document.user_id == user_id
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(NO_SUCH_DOCUMENT)
    return document


def documents_of_user(
    db: Session, *, user_id: int, limit: int = 50
) -> list[Document]:
    return list(
        db.execute(
            select(Document)
            .where(Document.user_id == user_id)
            .order_by(Document.created_at.desc())
            .limit(limit)
        ).scalars()
    )


def _one_orders(stmt, *, document_ids: list[int], kiosk_id: int, since: datetime):
    """Narrow a task query to one order's prints.

    A task names a document and a kiosk and never an order, so "this order's
    prints" has to be reconstructed -- and the same file printed at the same
    shop last week is the same document at the same kiosk. Without a bound, a
    reprint read last week's failure as its own and settled PARTIALLY_FAILED
    with every page in the student's hand. What separates the two is time: an
    order's tasks are made when it is paid, after it was placed, so nothing
    older than the order can be one of its prints.

    **`since` is the order's `created_at`, never its `paid_at`.** Both look
    right; only one is. `created_at` on both tables is Postgres's `now()` -- the
    start of the transaction, on one clock -- while `paid_at` is Python's clock
    read partway through, so a task made in the same transaction as the payment
    is *older* than `paid_at` and would be shut out of its own order.
    """
    return stmt.where(
        PrintTask.document_id.in_(document_ids),
        PrintTask.kiosk_id == kiosk_id,
        PrintTask.created_at >= since,
    )


def task_states_at(
    db: Session, *, document_ids: list[int], kiosk_id: int, since: datetime
) -> list[TaskState]:
    """What became of one order's prints.

    Asked by orders, which owns the question "is this whole order finished" and
    cannot answer it without knowing how each print went. States rather than
    tasks: orders has no business with a `PrintTask`, and returning one would
    invite it to start reading columns that are not its own.
    """
    if not document_ids:
        return []

    stmt = _one_orders(
        select(PrintTask.state),
        document_ids=document_ids,
        kiosk_id=kiosk_id,
        since=since,
    )
    return [TaskState(state) for state in db.execute(stmt).scalars()]


def document_task_states(
    db: Session, *, document_ids: list[int], kiosk_id: int, since: datetime
) -> dict[int, list[TaskState]]:
    """The same question, per document, for an operator reading one order.

    A list per document rather than one state: an order may carry the same file
    twice -- once in colour, once not -- and each line is its own print.
    """
    if not document_ids:
        return {}

    stmt = _one_orders(
        select(PrintTask.document_id, PrintTask.state),
        document_ids=document_ids,
        kiosk_id=kiosk_id,
        since=since,
    )
    found: dict[int, list[TaskState]] = {}
    for document_id, state in db.execute(stmt).all():
        found.setdefault(document_id, []).append(TaskState(state))
    return found


def failed_tasks_at(
    db: Session, *, document_ids: list[int], kiosk_id: int, since: datetime
) -> list[PrintTask]:
    """One order's failed prints, locked, in the order they were made.

    Locked because what happens next takes paper, and `consume_paper` dedupes
    nothing. Two operators pressing the same button at once would otherwise
    both read FAILED and both empty the tray; with the lock the second waits,
    Postgres re-reads the row once the first commits, and it is no longer
    failed.
    """
    if not document_ids:
        return []

    stmt = (
        _one_orders(
            select(PrintTask),
            document_ids=document_ids,
            kiosk_id=kiosk_id,
            since=since,
        )
        .where(PrintTask.state == TaskState.FAILED)
        .order_by(PrintTask.id)
        .with_for_update()
    )
    return list(db.execute(stmt).scalars())
