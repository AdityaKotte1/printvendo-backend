"""The claim, including a real concurrency test.

The duplicate-print bug in the old system was a race, so the test that matters
here uses two genuinely separate database transactions rather than mocks. A mock
cannot fail the way Postgres can.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.db import get_engine
from app.modules.identity.models import User
from app.modules.kiosks.models import Kiosk
from app.modules.printing.claims import (
    LEASE,
    claim_next_task,
    fail_expired,
    queue_depth,
    renew_held_lease,
    renew_lease,
)
from app.modules.printing.models import Document, PrintTask, TaskState


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    k = Kiosk(name="Queue Shop")
    db_session.add(k)
    db_session.flush()
    return k


@pytest.fixture
def document(db_session) -> Document:
    user = User(email="student@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    doc = Document(user_id=user.id, original_filename="notes.pdf", page_count=10)
    db_session.add(doc)
    db_session.flush()
    return doc


def _task(db_session, kiosk, document, *, position=0, sheets=10) -> PrintTask:
    task = PrintTask(
        document_id=document.id,
        kiosk_id=kiosk.id,
        position=position,
        predicted_sheets=sheets,
    )
    db_session.add(task)
    db_session.flush()
    return task


# ── the basics ──────────────────────────────────────────────────────────────


def test_claiming_an_empty_queue_returns_nothing(db_session, kiosk):
    assert claim_next_task(db_session, kiosk_id=kiosk.id) is None


def test_claiming_takes_the_task_out_of_the_queue(db_session, kiosk, document):
    task = _task(db_session, kiosk, document)

    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)
    assert claimed.id == task.id
    assert claimed.state is TaskState.SENT_TO_DEVICE


def test_a_claimed_task_is_not_handed_out_again(db_session, kiosk, document):
    """The single most important assertion in this module."""
    _task(db_session, kiosk, document)

    first = claim_next_task(db_session, kiosk_id=kiosk.id)
    second = claim_next_task(db_session, kiosk_id=kiosk.id)

    assert first is not None
    assert second is None


def test_claiming_sets_a_lease(db_session, kiosk, document):
    _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)

    assert claimed.claimed_at is not None
    assert claimed.lease_expires_at > datetime.now(UTC)


def test_the_returned_task_carries_every_value_the_claim_wrote(
    db_session, kiosk, document
):
    """The claim runs as SQL, so a copy of the row already in the session is
    stale afterwards. SQLAlchemy synchronises part of it back -- `state` and
    `attempts` arrive updated while `claimed_at` and `lease_expires_at` did not
    -- which is worse than none of it synchronising, because the object looks
    half-claimed and a caller reading its lease deadline silently gets None.

    Found by a device-reporting test, not by this file: every assertion here
    happened to read an attribute that survived.
    """
    task = _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)

    assert claimed is task, "the same identity-mapped object, hence the hazard"
    assert claimed.state is TaskState.SENT_TO_DEVICE
    assert claimed.attempts == 1
    assert claimed.claimed_at is not None
    assert claimed.lease_expires_at is not None


def test_claiming_counts_the_attempt(db_session, kiosk, document):
    _task(db_session, kiosk, document)
    assert claim_next_task(db_session, kiosk_id=kiosk.id).attempts == 1


def test_tasks_come_out_in_the_students_order(db_session, kiosk, document):
    """An order's files print in the order they were chosen, not insertion
    order."""
    _task(db_session, kiosk, document, position=2)
    _task(db_session, kiosk, document, position=0)
    _task(db_session, kiosk, document, position=1)

    order = [
        claim_next_task(db_session, kiosk_id=kiosk.id).position for _ in range(3)
    ]
    assert order == [0, 1, 2]


def test_one_kiosk_never_claims_anothers_work(db_session, kiosk, document):
    other = Kiosk(name="Other Shop")
    db_session.add(other)
    db_session.flush()
    _task(db_session, kiosk, document)

    assert claim_next_task(db_session, kiosk_id=other.id) is None


def test_queue_depth_counts_only_waiting_work(db_session, kiosk, document):
    """A claimed task is somebody's problem already; counting it would make the
    number a student sees jump around as devices pick work up."""
    _task(db_session, kiosk, document)
    _task(db_session, kiosk, document)
    assert queue_depth(db_session, kiosk_id=kiosk.id) == 2

    claim_next_task(db_session, kiosk_id=kiosk.id)
    assert queue_depth(db_session, kiosk_id=kiosk.id) == 1


# ── the race ────────────────────────────────────────────────────────────────


@pytest.fixture
def committed_queue(schema):
    """Real, committed rows that other connections can actually see.

    The `db_session` fixture runs inside an outer transaction that is rolled
    back at the end, so anything written through it -- including after a
    commit() -- stays invisible to other connections. That is exactly right for
    ordinary tests and useless for a concurrency test, which needs two
    connections to contend over the same committed row.

    Yields (kiosk_id, make_task) and deletes everything it created afterwards.
    """
    engine = get_engine(schema)
    setup = Session(engine)

    user = User(email="race@example.com", hashed_password="x")
    setup.add(user)
    setup.flush()

    doc = Document(user_id=user.id, original_filename="race.pdf", page_count=4)
    setup.add(doc)
    setup.flush()

    kiosk = Kiosk(name="Race Shop")
    setup.add(kiosk)
    setup.flush()

    created: list[int] = []

    def make_task(position: int = 0) -> int:
        task = PrintTask(
            document_id=doc.id,
            kiosk_id=kiosk.id,
            position=position,
            predicted_sheets=4,
        )
        setup.add(task)
        setup.flush()
        created.append(task.id)
        return task.id

    kiosk_id = kiosk.id
    try:
        yield kiosk_id, make_task, setup
    finally:
        cleanup = Session(engine)
        cleanup.query(PrintTask).filter(PrintTask.kiosk_id == kiosk_id).delete()
        cleanup.query(Kiosk).filter(Kiosk.id == kiosk_id).delete()
        cleanup.query(Document).filter(Document.id == doc.id).delete()
        cleanup.query(User).filter(User.id == user.id).delete()
        cleanup.commit()
        cleanup.close()
        setup.close()


def test_two_concurrent_transactions_cannot_claim_the_same_task(committed_queue):
    """The duplicate print, reproduced properly.

    Two separate connections asking for work at the same moment, with the first
    still holding its transaction open. Without SKIP LOCKED the second would
    block on the row lock and then return the same task; with it, the second
    skips the locked row and finds nothing else.

    Real transactions on purpose -- a mock cannot fail the way Postgres can.
    """
    kiosk_id, make_task, setup = committed_queue
    make_task()
    setup.commit()

    engine = setup.get_bind()
    first, second = Session(engine), Session(engine)

    try:
        claimed_a = claim_next_task(first, kiosk_id=kiosk_id)
        # First transaction deliberately left open, still holding its row lock.
        claimed_b = claim_next_task(second, kiosk_id=kiosk_id)

        assert claimed_a is not None, "the first caller should get the task"
        assert claimed_b is None, "the second caller must not get the same task"

        first.commit()
        second.commit()
    finally:
        first.close()
        second.close()


def test_two_concurrent_transactions_take_different_tasks(committed_queue):
    """With two tasks waiting, both callers get served: SKIP LOCKED moves to the
    next row rather than blocking."""
    kiosk_id, make_task, setup = committed_queue
    make_task(position=0)
    make_task(position=1)
    setup.commit()

    engine = setup.get_bind()
    first, second = Session(engine), Session(engine)

    try:
        claimed_a = claim_next_task(first, kiosk_id=kiosk_id)
        claimed_b = claim_next_task(second, kiosk_id=kiosk_id)

        assert claimed_a is not None
        assert claimed_b is not None
        assert claimed_a.id != claimed_b.id

        first.commit()
        second.commit()
    finally:
        first.close()
        second.close()


# ── leases, and what happens when one runs out ─────────────────────────────
#
# A job whose machine went quiet is failed, never handed out again. The machine
# may have printed it: a Windows kiosk whose printer was out of paper held a job
# in its spooler past the lease, was handed the same job again and spooled a
# second copy, then a third -- and every copy came out when paper went in.


def test_a_live_lease_is_left_alone(db_session, kiosk, document):
    _task(db_session, kiosk, document)
    claim_next_task(db_session, kiosk_id=kiosk.id)

    assert fail_expired(db_session) == []


def test_an_expired_lease_fails_the_task(db_session, kiosk, document):
    _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)
    claimed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    lost = fail_expired(db_session)

    assert [t.id for t in lost] == [claimed.id]
    assert claimed.state is TaskState.FAILED
    assert claimed.error_code == "LEASE_EXPIRED"
    assert claimed.lease_expires_at is None
    assert claimed.finished_at is not None


def test_a_task_whose_lease_ran_out_is_never_handed_out_again(
    db_session, kiosk, document
):
    """The defect, as a test. Handing it out again is how one shop printed the
    same jobs over and over with no new orders."""
    _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)
    claimed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    fail_expired(db_session)

    assert claim_next_task(db_session, kiosk_id=kiosk.id) is None


def test_renewing_a_lease_keeps_a_long_job_alive(db_session, kiosk, document):
    _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)

    claimed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()
    renew_lease(db_session, claimed)

    assert fail_expired(db_session) == []
    assert claimed.lease_expires_at > datetime.now(UTC)


def test_a_finished_task_is_never_touched(db_session, kiosk, document):
    _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)
    claimed.state = TaskState.PRINTED
    claimed.lease_expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.flush()

    assert fail_expired(db_session) == []
    assert claimed.state is TaskState.PRINTED


def test_the_machine_holding_a_task_renews_its_lease(db_session, kiosk, document):
    """What the heartbeat does when it names the job in hand, so a job waiting
    behind an empty tray is not mistaken for a lost one."""
    _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)
    claimed.lease_expires_at = datetime.now(UTC) + timedelta(seconds=5)
    db_session.flush()

    renewed = renew_held_lease(
        db_session, kiosk_id=kiosk.id, task_public_id=claimed.public_id
    )

    assert renewed is True
    assert claimed.lease_expires_at > datetime.now(UTC) + timedelta(minutes=10)


def test_a_task_is_renewed_only_by_its_own_kiosk(db_session, kiosk, document):
    _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)
    before = claimed.lease_expires_at

    renewed = renew_held_lease(
        db_session, kiosk_id=kiosk.id + 999, task_public_id=claimed.public_id
    )

    assert renewed is False
    assert claimed.lease_expires_at == before


def test_a_finished_task_is_not_renewed(db_session, kiosk, document):
    _task(db_session, kiosk, document)
    claimed = claim_next_task(db_session, kiosk_id=kiosk.id)
    claimed.state = TaskState.PRINTED
    claimed.lease_expires_at = None
    db_session.flush()

    renewed = renew_held_lease(
        db_session, kiosk_id=kiosk.id, task_public_id=claimed.public_id
    )

    assert renewed is False
    assert claimed.lease_expires_at is None


def test_renewing_an_unknown_task_is_not_an_error(db_session, kiosk):
    assert (
        renew_held_lease(db_session, kiosk_id=kiosk.id, task_public_id="tsk_nothing")
        is False
    )


def test_the_lease_is_generous_enough_for_a_real_print_job():
    """A large colour job on a slow kiosk printer takes minutes."""
    assert LEASE >= timedelta(minutes=10)
