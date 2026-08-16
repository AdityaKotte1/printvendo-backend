"""Taking a file from a student, and letting go of it again.

The order of operations at upload is deliberate: **validate, then store, then
record**. The backend being replaced wrote the row first, then ran its
validation, then swallowed the failure -- so a file it had already decided was
unprintable still reached a printer.

Retention keys on the *task*, not on the document's age alone. A file deleted
out from under a job that has not printed yet is a student who paid and got
nothing, and no amount of age makes that acceptable.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.modules.printing.models import (
    TERMINAL_TASK_STATES,
    Document,
    DocumentState,
    PrintTask,
)
from app.modules.printing.pdfs import inspect_pdf, normalise_pdf, should_normalise
from app.modules.printing.storage import DocumentStore, StorageArea

logger = logging.getLogger(__name__)

NO_FILE = "That document is no longer available. Please upload it again."


def create_document(
    db: Session,
    store: DocumentStore,
    *,
    user_id: int,
    filename: str,
    data: bytes,
    max_bytes: int | None = None,
    max_pages: int | None = None,
) -> Document:
    """Accept an upload, or raise with a sentence explaining why not.

    Nothing is written -- no row, no file -- unless the PDF is printable. A
    rejected upload therefore leaves no trace to clean up and nothing that could
    later be handed to a printer.
    """
    facts = inspect_pdf(data, max_bytes=max_bytes, max_pages=max_pages)

    key = store.save(
        StorageArea.ORIGINAL, user_id=user_id, filename=filename, data=data
    )

    document = Document(
        user_id=user_id,
        original_filename=filename,
        original_path=key,
        page_count=facts.page_count,
        byte_size=facts.byte_size,
        state=DocumentState.READY,
    )
    db.add(document)
    db.flush()
    return document


def printable_key(document: Document) -> str:
    """The storage key of the file a printer should be given.

    The normalised copy when there is one, the original otherwise. Never a
    page-trimmed file: the page range is applied at the printer, and trimming
    here too would apply it twice.
    """
    key = document.normalised_path or document.original_path
    if not key:
        raise NotFound(NO_FILE)
    return key


def normalise_document(
    db: Session, store: DocumentStore, document: Document
) -> bool:
    """Shrink the file, if it is worth shrinking. True if a copy was made.

    Runs after the upload has been answered, never inside it. `original_path` is
    always printable, so a device that fetches the file before this finishes --
    or instead of it -- gets something correct either way.
    """
    if not document.original_path:
        return False

    source = store.path(document.original_path)
    if not should_normalise(source):
        return False

    key = store.new_key(
        StorageArea.NORMALISED,
        user_id=document.user_id,
        filename=document.original_filename,
    )
    if not normalise_pdf(source, store.path(key)):
        return False

    document.normalised_path = key
    db.add(document)
    return True


def normalise_pending(
    db: Session,
    store: DocumentStore,
    *,
    limit: int = 20,
) -> list[Document]:
    """Shrink documents that are worth shrinking and have not been yet.

    A sweep rather than work scheduled after each upload. The alternative --
    a background task fired from the request -- needs its own database session
    because the request's is closed by then, and that is a class of bug worth
    not having for an optimisation. Nothing waits on this: `original_path` is
    always printable, so a device that fetches a file before the sweep runs
    gets something correct.
    """
    candidates = list(
        db.execute(
            select(Document)
            .where(
                Document.state == DocumentState.READY,
                Document.normalised_path.is_(None),
                Document.original_path.is_not(None),
            )
            .order_by(Document.created_at)
            .limit(limit)
        ).scalars()
    )

    return [
        document
        for document in candidates
        if normalise_document(db, store, document)
    ]


def delete_document(db: Session, store: DocumentStore, document: Document) -> None:
    """Remove a document a student no longer wants.

    Refused while anything is still waiting to print it. Deleting the file out
    from under a queued task is the "paid and got nothing" failure, and a
    student tidying their list must not be able to cause it.
    """
    waiting = db.execute(
        select(PrintTask.id).where(
            PrintTask.document_id == document.id,
            PrintTask.state.not_in(TERMINAL_TASK_STATES),
        )
    ).first()
    if waiting is not None:
        raise Conflict(
            "That document is still waiting to print. It can be deleted once "
            "the job has finished."
        )

    for key in (document.original_path, document.normalised_path):
        if key:
            store.delete(key)
    db.delete(document)


def purge_expired_files(
    db: Session,
    store: DocumentStore,
    *,
    older_than: timedelta,
    now: datetime | None = None,
) -> list[Document]:
    """Delete the files of documents nothing is waiting on any more.

    The row survives, marked EXPIRED, so a student's order history keeps its
    shape. Both artefacts go together -- the old backend removed the converted
    file on the PRINTED path only, orphaning a `.gz` sibling for every job that
    failed.

    Re-running is harmless: an already-expired document is not selected again.
    """
    now = now or datetime.now(UTC)
    cutoff = now - older_than

    # A document with any task that has not finished is still needed, whatever
    # its age. Expressed as "not in the set of blocked documents" rather than as
    # a join, so a document with three tasks is considered once.
    still_wanted = select(PrintTask.document_id).where(
        PrintTask.state.not_in(TERMINAL_TASK_STATES)
    )

    stale = list(
        db.execute(
            select(Document).where(
                Document.created_at < cutoff,
                Document.state != DocumentState.EXPIRED,
                Document.id.not_in(still_wanted),
            )
        ).scalars()
    )

    for document in stale:
        for key in (document.original_path, document.normalised_path):
            if key:
                store.delete(key)
        document.original_path = None
        document.normalised_path = None
        document.state = DocumentState.EXPIRED
        db.add(document)

    if stale:
        logger.info("retention: purged files for %s documents", len(stale))
    return stale
