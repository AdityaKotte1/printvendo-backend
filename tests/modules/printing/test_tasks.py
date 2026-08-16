"""What a device reports back, and what it is allowed to change.

The paper rules live here because this is where a print finishes. The backend
being replaced deducted its own estimate, and only on success, so a job that
jammed after three sheets deducted zero -- which is why a kiosk can report paper
remaining while its tray is empty.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import Conflict
from app.modules.printing.claims import LEASE, claim_next_task
from app.modules.printing.models import Document, PrintTask, TaskState
from app.modules.printing.tasks import (
    ALREADY_FINISHED,
    NOT_IN_HAND,
    report_blocked,
    report_failed,
    report_printed,
    start_printing,
)


@dataclass
class FakeLedger:
    """Records what would have been deducted, so a paper rule can be asserted
    without dragging the kiosks module into a printing test."""

    calls: list[dict] = field(default_factory=list)

    def consume(self, db, kiosk_id, *, predicted_sheets, actual_sheets, reference):
        self.calls.append(
            {
                "kiosk_id": kiosk_id,
                "predicted": predicted_sheets,
                "actual": actual_sheets,
                "reference": reference,
            }
        )

    @property
    def deducted(self) -> int | None:
        """What the tray actually loses: the reported figure when there is one."""
        last = self.calls[-1]
        return last["actual"] if last["actual"] is not None else last["predicted"]


@pytest.fixture
def ledger() -> FakeLedger:
    return FakeLedger()


@pytest.fixture
def kiosk(db_session):
    from app.modules.kiosks.models import Kiosk

    kiosk = Kiosk(name="Task Report Shop")
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


@pytest.fixture
def document(db_session):
    from app.modules.identity.models import User

    user = User(email="tasks@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    doc = Document(user_id=user.id, original_filename="report.pdf", page_count=10)
    db_session.add(doc)
    db_session.flush()
    return doc


@pytest.fixture
def claimed(db_session, kiosk, document) -> PrintTask:
    task = PrintTask(
        document_id=document.id,
        kiosk_id=kiosk.id,
        position=0,
        predicted_sheets=6,
    )
    db_session.add(task)
    db_session.flush()
    return claim_next_task(db_session, kiosk_id=kiosk.id)


# ── progress ────────────────────────────────────────────────────────────────


def test_a_device_that_starts_printing_says_so(db_session, claimed):
    start_printing(db_session, claimed)

    assert claimed.state is TaskState.PRINTING
    assert claimed.started_at is not None


def test_starting_extends_the_lease(db_session, claimed):
    """A long job must not be requeued underneath the machine printing it --
    that is the duplicate this whole module exists to prevent."""
    before = claimed.lease_expires_at
    later = datetime.now(UTC) + timedelta(minutes=5)

    start_printing(db_session, claimed, now=later)

    assert claimed.lease_expires_at > before
    assert claimed.lease_expires_at == later + LEASE


# ── finishing, and the paper ────────────────────────────────────────────────


def test_a_finished_print_deducts_what_the_printer_reported(db_session, claimed, ledger):
    report_printed(db_session, claimed, ledger, sheets_used=7)

    assert claimed.state is TaskState.PRINTED
    assert claimed.finished_at is not None
    assert ledger.deducted == 7


def test_the_reported_figure_is_kept_for_comparison(db_session, claimed, ledger):
    """Predicted six, used seven: that difference is a driver ignoring duplex,
    and it is worth seeing rather than absorbing."""
    report_printed(db_session, claimed, ledger, sheets_used=7)

    assert claimed.actual_sheets == 7
    assert ledger.calls[-1]["predicted"] == 6
    assert ledger.calls[-1]["actual"] == 7


def test_an_agent_too_old_to_report_falls_back_to_the_prediction(
    db_session, claimed, ledger
):
    report_printed(db_session, claimed, ledger, sheets_used=None)

    assert ledger.deducted == 6
    assert claimed.actual_sheets is None


def test_a_print_that_failed_halfway_still_deducts_what_it_used(
    db_session, claimed, ledger
):
    """Half a job still empties half a tray. The old backend deducted nothing
    unless the job reached PRINTED."""
    report_failed(db_session, claimed, ledger, sheets_used=3, error_code="JAM")

    assert claimed.state is TaskState.FAILED
    assert ledger.deducted == 3


def test_a_failure_with_no_figure_does_not_invent_one(db_session, claimed, ledger):
    """Deducting the full prediction for a job that may never have started
    would be a guess in the other direction. Zero is recorded against the
    prediction, so the refill log shows the discrepancy instead of hiding it."""
    report_failed(db_session, claimed, ledger, sheets_used=None, error_code="OFFLINE")

    assert ledger.deducted == 0
    assert ledger.calls[-1]["predicted"] == 6


def test_the_failure_reason_is_kept(db_session, claimed, ledger):
    report_failed(
        db_session,
        claimed,
        ledger,
        sheets_used=0,
        error_code="NO_PAPER",
        error_message="Tray 1 is empty",
    )

    assert claimed.error_code == "NO_PAPER"
    assert claimed.error_message == "Tray 1 is empty"


def test_the_deduction_names_the_task_so_a_refill_log_line_can_be_traced(
    db_session, claimed, ledger
):
    report_printed(db_session, claimed, ledger, sheets_used=6)

    assert claimed.public_id in ledger.calls[-1]["reference"]


# ── reporting twice must not charge the tray twice ──────────────────────────


def test_a_finished_task_cannot_be_finished_again(db_session, claimed, ledger):
    """An agent that retries its status call after a network timeout must not
    empty the tray a second time."""
    report_printed(db_session, claimed, ledger, sheets_used=6)

    with pytest.raises(Conflict) as exc:
        report_printed(db_session, claimed, ledger, sheets_used=6)

    # The specific sentence matters: a retry after a network timeout is benign
    # and the agent should learn it already finished, not that its task belongs
    # to someone else.
    assert str(exc.value) == ALREADY_FINISHED
    assert len(ledger.calls) == 1


def test_a_failed_task_cannot_then_be_reported_printed(db_session, claimed, ledger):
    report_failed(db_session, claimed, ledger, sheets_used=2, error_code="JAM")

    with pytest.raises(Conflict) as exc:
        report_printed(db_session, claimed, ledger, sheets_used=6)

    assert str(exc.value) == ALREADY_FINISHED
    assert len(ledger.calls) == 1


def test_a_task_nobody_claimed_cannot_report_progress(db_session, kiosk, document):
    """Only the device holding the claim may move a task. A report about a
    queued task is either a bug or somebody else's device."""
    queued = PrintTask(
        document_id=document.id, kiosk_id=kiosk.id, position=0, predicted_sheets=2
    )
    db_session.add(queued)
    db_session.flush()

    with pytest.raises(Conflict) as exc:
        start_printing(db_session, queued)

    assert str(exc.value) == NOT_IN_HAND


def test_a_task_nobody_claimed_cannot_be_reported_printed(
    db_session, kiosk, document, ledger
):
    queued = PrintTask(
        document_id=document.id, kiosk_id=kiosk.id, position=0, predicted_sheets=2
    )
    db_session.add(queued)
    db_session.flush()

    with pytest.raises(Conflict) as exc:
        report_printed(db_session, queued, ledger, sheets_used=2)

    assert str(exc.value) == NOT_IN_HAND
    assert ledger.calls == []


# ── blocked ─────────────────────────────────────────────────────────────────


def test_a_device_can_refuse_a_task_without_consuming_paper(db_session, claimed, ledger):
    """Out of paper, printer offline, wrong media: nothing was printed, so
    nothing is deducted, and the task is not failed either -- it can run once
    the tray is filled."""
    report_blocked(db_session, claimed, reason="NO_PAPER", message="Tray is empty")

    assert claimed.state is TaskState.BLOCKED
    assert claimed.error_code == "NO_PAPER"
    assert ledger.calls == []


def test_a_blocked_task_holds_no_lease(db_session, claimed):
    """It is not being worked on, so the sweeper has nothing to recover. Leaving
    a lease would requeue it behind the operator's back."""
    report_blocked(db_session, claimed, reason="NO_PAPER")

    assert claimed.lease_expires_at is None


def test_negative_sheets_are_refused(db_session, claimed, ledger):
    """A device reporting -3 sheets would credit the tray."""
    from app.core.errors import BadRequest

    with pytest.raises(BadRequest):
        report_printed(db_session, claimed, ledger, sheets_used=-3)

    assert ledger.calls == []
