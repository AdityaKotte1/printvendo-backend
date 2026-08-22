"""Putting work into a kiosk's queue, and knowing what is already there.

Both exist so the orders module never has to touch a `PrintTask` row itself --
printing owns its tables, and an aggregate that reached into them would be the
same shape as the old backend, where three tables were written from everywhere.
"""

import pytest

from app.core.bus import WAKE_KEY
from app.modules.printing import PrintOptions
from app.modules.printing.enqueue import committed_sheets, enqueue_task
from app.modules.printing.models import PrintTask, TaskState


@pytest.fixture
def kiosk(db_session):
    from app.modules.kiosks.models import Kiosk

    kiosk = Kiosk(name="Enqueue Shop")
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def document(db_session):
    from app.modules.identity.models import User
    from app.modules.printing.models import Document

    user = User(email="enqueue@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    doc = Document(user_id=user.id, original_filename="q.pdf", page_count=10)
    db_session.add(doc)
    db_session.flush()
    return doc


def options(**kwargs) -> PrintOptions:
    defaults = {"total_pages": 10, "colour": False, "duplex": False, "copies": 1}
    return PrintOptions.create(**{**defaults, **kwargs})


# ── enqueueing ──────────────────────────────────────────────────────────────


def test_a_queued_task_carries_the_settings_that_were_paid_for(
    db_session, kiosk, document
):
    task = enqueue_task(
        db_session,
        document_id=document.id,
        kiosk_id=kiosk.id,
        options=options(colour=True, duplex=True, copies=2, page_range="1,4-6"),
        total_pages=10,
        position=0,
    )

    assert task.state is TaskState.QUEUED
    assert task.colour is True
    assert task.duplex is True
    assert task.copies == 2
    assert task.page_range == "1,4-6"


def test_the_predicted_sheets_come_from_the_one_calculation(
    db_session, kiosk, document
):
    """Four selected pages, two copies, duplex: two sheets per copy, four in all.
    The same function priced it and the same function will be compared against
    what the printer reports."""
    task = enqueue_task(
        db_session,
        document_id=document.id,
        kiosk_id=kiosk.id,
        options=options(duplex=True, copies=2, page_range="1,4-6"),
        total_pages=10,
        position=0,
    )

    assert task.predicted_sheets == 4


def test_position_is_kept_so_files_print_in_the_students_order(
    db_session, kiosk, document
):
    second = enqueue_task(
        db_session,
        document_id=document.id,
        kiosk_id=kiosk.id,
        options=options(),
        total_pages=10,
        position=1,
    )

    assert second.position == 1


def test_a_queued_task_holds_no_lease(db_session, kiosk, document):
    """Only a claim creates one. A task that arrived with a lease would be
    requeued by the sweeper without ever having been handed to anything."""
    task = enqueue_task(
        db_session,
        document_id=document.id,
        kiosk_id=kiosk.id,
        options=options(),
        total_pages=10,
        position=0,
    )

    assert task.lease_expires_at is None
    assert task.claimed_at is None
    assert task.attempts == 0


# ── what the queue has already promised ─────────────────────────────────────


def _task(db_session, kiosk, document, *, sheets: int, state=TaskState.QUEUED):
    task = PrintTask(
        document_id=document.id,
        kiosk_id=kiosk.id,
        position=0,
        predicted_sheets=sheets,
        state=state,
    )
    db_session.add(task)
    db_session.flush()
    return task


def test_an_empty_queue_has_promised_nothing(db_session, kiosk):
    assert committed_sheets(db_session, kiosk_id=kiosk.id) == 0


def test_queued_work_counts_against_the_tray(db_session, kiosk, document):
    """Paper is reserved by derivation. Two queued jobs of 30 sheets have
    already spoken for 60, whether or not either has started."""
    _task(db_session, kiosk, document, sheets=30)
    _task(db_session, kiosk, document, sheets=30)

    assert committed_sheets(db_session, kiosk_id=kiosk.id) == 60


def test_work_in_progress_still_counts(db_session, kiosk, document):
    _task(db_session, kiosk, document, sheets=10, state=TaskState.SENT_TO_DEVICE)
    _task(db_session, kiosk, document, sheets=10, state=TaskState.PRINTING)

    assert committed_sheets(db_session, kiosk_id=kiosk.id) == 20


def test_finished_work_no_longer_counts(db_session, kiosk, document):
    """A printed job has already been deducted from the tray by the device
    report. Counting it here as well would reserve the same paper twice and a
    kiosk would refuse orders it can perfectly well print."""
    _task(db_session, kiosk, document, sheets=40, state=TaskState.PRINTED)
    _task(db_session, kiosk, document, sheets=40, state=TaskState.FAILED)
    _task(db_session, kiosk, document, sheets=40, state=TaskState.CANCELLED)

    assert committed_sheets(db_session, kiosk_id=kiosk.id) == 0


def test_blocked_work_still_counts(db_session, kiosk, document):
    """Blocked is not finished -- it runs as soon as the tray is filled, so its
    paper is still spoken for."""
    _task(db_session, kiosk, document, sheets=15, state=TaskState.BLOCKED)

    assert committed_sheets(db_session, kiosk_id=kiosk.id) == 15


def test_another_kiosks_queue_is_not_counted(db_session, kiosk, document):
    from app.modules.kiosks.models import Kiosk

    other = Kiosk(name="Somebody Else's Shop")
    db_session.add(other)
    db_session.flush()
    _task(db_session, other, document, sheets=100)

    assert committed_sheets(db_session, kiosk_id=kiosk.id) == 0


# ── telling the shop there is work ──────────────────────────────────────────


def test_queueing_a_task_marks_its_kiosk_to_be_woken(db_session, kiosk, document):
    """Noted where the work is created, not at whichever route caused it. A
    route that queues a task cannot forget to wake the shop, because it does not
    know it is doing it -- and "remember to call the helper" is what produced an
    audit trail covering 15 of 94 mutating routes in the old backend.

    The wake itself is sent after this transaction commits; see
    `core.bus.flush_wakes`, wired into `get_db`.
    """
    enqueue_task(
        db_session,
        document_id=document.id,
        kiosk_id=kiosk.id,
        options=options(),
        total_pages=10,
        position=0,
    )

    assert db_session.info[WAKE_KEY] == {kiosk.id}
