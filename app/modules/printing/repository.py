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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.printing.models import Document, PrintTask

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
