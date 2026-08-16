"""Taking a file from a student, and letting go of it again."""

import io
from datetime import UTC, datetime, timedelta

import pikepdf
import pytest

from app.core.errors import BadRequest, NotFound
from app.modules.printing.documents import (
    create_document,
    normalise_document,
    printable_key,
    purge_expired_files,
)
from app.modules.printing.models import Document, DocumentState, PrintTask, TaskState
from app.modules.printing.storage import DocumentStore, StorageArea


def make_pdf(pages: int = 2) -> bytes:
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(595, 842))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    return DocumentStore(tmp_path / "storage")


@pytest.fixture
def user(db_session):
    from app.modules.identity.models import User

    user = User(email="docs@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def kiosk(db_session):
    from app.modules.kiosks.models import Kiosk

    kiosk = Kiosk(name="Documents Shop")
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


# ── accepting an upload ─────────────────────────────────────────────────────


def test_an_accepted_upload_is_ready_to_print(db_session, store, user):
    document = create_document(
        db_session, store, user_id=user.id, filename="essay.pdf", data=make_pdf(3)
    )

    assert document.state is DocumentState.READY
    assert document.page_count == 3
    assert document.original_filename == "essay.pdf"


def test_the_file_is_written_where_the_row_says_it_is(db_session, store, user):
    data = make_pdf(1)
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=data
    )

    assert store.read(document.original_path) == data
    assert document.byte_size == len(data)


def test_the_original_filename_is_kept_but_never_used_as_a_path(
    db_session, store, user
):
    """What the student called it is worth showing back to them. It is not worth
    trusting on the filesystem."""
    document = create_document(
        db_session,
        store,
        user_id=user.id,
        filename="../../etc/passwd.pdf",
        data=make_pdf(1),
    )

    assert document.original_filename == "../../etc/passwd.pdf"
    assert ".." not in document.original_path


def test_a_rejected_upload_leaves_no_row_and_no_file(db_session, store, user):
    """A file that cannot be printed is not a document. The old backend wrote
    the row first and then swallowed the validation error, so unprintable jobs
    sat in the queue."""
    before = db_session.query(Document).count()

    with pytest.raises(BadRequest):
        create_document(
            db_session, store, user_id=user.id, filename="x.pdf", data=b"not a pdf"
        )

    assert db_session.query(Document).count() == before
    assert not list(store.root.rglob("*.pdf"))


def test_an_oversized_upload_is_refused(db_session, store, user):
    with pytest.raises(BadRequest):
        create_document(
            db_session,
            store,
            user_id=user.id,
            filename="x.pdf",
            data=make_pdf(1),
            max_bytes=10,
        )


# ── what actually gets printed ──────────────────────────────────────────────


def test_the_original_is_printed_when_there_is_nothing_better(
    db_session, store, user
):
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )

    assert printable_key(document) == document.original_path


def test_the_normalised_copy_is_preferred_once_it_exists(db_session, store, user):
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )
    document.normalised_path = "normalised/2026/08/x.pdf"

    assert printable_key(document) == document.normalised_path


def test_a_document_whose_file_has_been_purged_has_nothing_to_print(
    db_session, store, user
):
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )
    document.state = DocumentState.EXPIRED
    document.original_path = None

    with pytest.raises(NotFound):
        printable_key(document)


def test_normalising_a_small_document_is_skipped_rather_than_attempted(
    db_session, store, user
):
    """Ghostscript on a 4kB PDF costs more than it saves."""
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )

    assert normalise_document(db_session, store, document) is False
    assert document.normalised_path is None


def test_normalising_a_large_document_produces_a_second_file(
    db_session, store, user, monkeypatch
):
    from PIL import Image

    noise = Image.effect_noise((1600, 2200), 96).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, format="PDF", resolution=600.0)

    document = create_document(
        db_session, store, user_id=user.id, filename="photo.pdf", data=buffer.getvalue()
    )
    monkeypatch.setattr(
        "app.modules.printing.documents.should_normalise", lambda path: True
    )

    assert normalise_document(db_session, store, document) is True
    assert document.normalised_path is not None
    assert document.normalised_path.startswith(StorageArea.NORMALISED.value)
    assert store.exists(document.normalised_path)


def test_the_original_survives_normalisation(db_session, store, user, monkeypatch):
    """It is what a reprint or a dispute goes back to."""
    from PIL import Image

    noise = Image.effect_noise((1600, 2200), 96).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, format="PDF", resolution=600.0)

    document = create_document(
        db_session, store, user_id=user.id, filename="photo.pdf", data=buffer.getvalue()
    )
    monkeypatch.setattr(
        "app.modules.printing.documents.should_normalise", lambda path: True
    )
    normalise_document(db_session, store, document)

    assert store.exists(document.original_path)


# ── retention ───────────────────────────────────────────────────────────────


def _age(db_session, document, days: int) -> None:
    db_session.flush()
    document.created_at = datetime.now(UTC) - timedelta(days=days)
    db_session.flush()


def test_an_old_document_loses_its_file_but_keeps_its_row(db_session, store, user):
    """The row outlives the file so a student's order history does not develop
    holes where a print used to be."""
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )
    key = document.original_path
    _age(db_session, document, days=30)

    purged = purge_expired_files(db_session, store, older_than=timedelta(days=7))

    assert document in purged
    assert document.state is DocumentState.EXPIRED
    assert not store.exists(key)
    assert db_session.get(Document, document.id) is not None


def test_a_recent_document_is_left_alone(db_session, store, user):
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )

    purge_expired_files(db_session, store, older_than=timedelta(days=7))

    assert document.state is DocumentState.READY
    assert store.exists(document.original_path)


def test_a_document_still_waiting_to_print_is_never_purged(
    db_session, store, user, kiosk
):
    """Retention keys on the task, not the age. Deleting the file under a job
    that has not printed yet is how a student pays and gets nothing."""
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )
    db_session.add(
        PrintTask(
            document_id=document.id,
            kiosk_id=kiosk.id,
            position=0,
            predicted_sheets=1,
            state=TaskState.QUEUED,
        )
    )
    _age(db_session, document, days=30)

    purge_expired_files(db_session, store, older_than=timedelta(days=7))

    assert document.state is DocumentState.READY
    assert store.exists(document.original_path)


def test_a_document_whose_task_has_finished_is_purged(db_session, store, user, kiosk):
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )
    db_session.add(
        PrintTask(
            document_id=document.id,
            kiosk_id=kiosk.id,
            position=0,
            predicted_sheets=1,
            state=TaskState.PRINTED,
        )
    )
    _age(db_session, document, days=30)

    purge_expired_files(db_session, store, older_than=timedelta(days=7))

    assert document.state is DocumentState.EXPIRED


def test_purging_removes_the_normalised_copy_too(
    db_session, store, user, monkeypatch
):
    """Both artefacts or neither. The old backend left `.gz` siblings behind on
    every job that did not reach PRINTED."""
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )
    normalised = store.save(
        StorageArea.NORMALISED, user_id=user.id, filename="a.pdf", data=b"%PDF-small"
    )
    document.normalised_path = normalised
    _age(db_session, document, days=30)

    purge_expired_files(db_session, store, older_than=timedelta(days=7))

    assert not store.exists(normalised)
    assert document.normalised_path is None


def test_purging_twice_is_harmless(db_session, store, user):
    """The sweep runs on a schedule over the same rows."""
    document = create_document(
        db_session, store, user_id=user.id, filename="a.pdf", data=make_pdf(1)
    )
    _age(db_session, document, days=30)

    purge_expired_files(db_session, store, older_than=timedelta(days=7))
    again = purge_expired_files(db_session, store, older_than=timedelta(days=7))

    assert again == []
