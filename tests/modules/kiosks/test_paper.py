import pytest

from app.core.errors import BadRequest
from app.modules.kiosks.models import Kiosk, KioskPaper, PaperRefillLog
from app.modules.kiosks.paper import (
    mark_out_of_paper,
    reset_paper,
    set_paper,
    sheets_remaining,
)


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    k = Kiosk(name="Shop")
    db_session.add(k)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=k.id, capacity=250, used=200))
    db_session.flush()
    return k


def _logs(db_session, kiosk) -> list[PaperRefillLog]:
    return db_session.query(PaperRefillLog).filter_by(kiosk_id=kiosk.id).all()


def test_sheets_remaining_is_capacity_minus_used(db_session, kiosk):
    assert sheets_remaining(db_session, kiosk) == 50


def test_reset_refills_the_tray(db_session, kiosk):
    reset_paper(db_session, kiosk, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == 250


def test_reset_writes_a_log_row(db_session, kiosk):
    reset_paper(db_session, kiosk, actor_user_id=None)
    db_session.flush()
    assert len(_logs(db_session, kiosk)) == 1


def test_reset_clears_the_warning_throttle(db_session, kiosk):
    """Fresh paper must re-arm the alerts. Miss this and refilling silently
    disables low-paper warnings for that kiosk."""
    from datetime import UTC, datetime

    paper = db_session.get(KioskPaper, kiosk.id)
    paper.warning_count = 3
    paper.last_warning_at = datetime.now(UTC)
    paper.last_reminder_at = datetime.now(UTC)
    db_session.flush()

    reset_paper(db_session, kiosk, actor_user_id=None)
    db_session.flush()

    assert paper.warning_count == 0
    assert paper.last_warning_at is None
    assert paper.last_reminder_at is None


def test_set_sheets_left_directly(db_session, kiosk):
    """A refiller rarely fills a tray to the top."""
    set_paper(db_session, kiosk, sheets_left=120, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == 120


def test_changing_capacity_alone_preserves_sheets_remaining(db_session, kiosk):
    """Resizing a tray does not add or remove paper."""
    before = sheets_remaining(db_session, kiosk)
    set_paper(db_session, kiosk, capacity=500, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == before


def test_capacity_and_fill_can_be_set_together(db_session, kiosk):
    set_paper(db_session, kiosk, capacity=500, sheets_left=400, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == 400


def test_set_with_neither_argument_is_refused(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, actor_user_id=None)


def test_sheets_left_cannot_exceed_capacity(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, sheets_left=9999, actor_user_id=None)


def test_sheets_left_cannot_be_negative(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, sheets_left=-1, actor_user_id=None)


def test_capacity_must_be_positive(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, capacity=0, actor_user_id=None)


def test_shrinking_the_tray_below_its_contents_is_refused(db_session, kiosk):
    """250-sheet tray holding 50; shrinking to 20 would leave more paper in it
    than it can hold."""
    with pytest.raises(BadRequest):
        set_paper(db_session, kiosk, capacity=20, actor_user_id=None)


def test_topping_up_clears_the_throttle_but_using_paper_does_not(db_session, kiosk):
    paper = db_session.get(KioskPaper, kiosk.id)
    paper.warning_count = 2
    db_session.flush()

    set_paper(db_session, kiosk, sheets_left=10, actor_user_id=None)  # less than 50
    assert paper.warning_count == 2

    set_paper(db_session, kiosk, sheets_left=200, actor_user_id=None)  # a refill
    assert paper.warning_count == 0


def test_mark_out_of_paper_empties_the_tray(db_session, kiosk):
    mark_out_of_paper(db_session, kiosk, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == 0


def test_every_change_is_logged(db_session, kiosk):
    reset_paper(db_session, kiosk, actor_user_id=None)
    set_paper(db_session, kiosk, sheets_left=10, actor_user_id=None)
    mark_out_of_paper(db_session, kiosk, actor_user_id=None)
    db_session.flush()

    assert len(_logs(db_session, kiosk)) == 3


def test_the_log_records_who_did_it(db_session, kiosk):
    from app.modules.identity.models import User

    actor = User(email="refiller@example.com", hashed_password="x")
    db_session.add(actor)
    db_session.flush()

    reset_paper(db_session, kiosk, actor_user_id=actor.id)
    db_session.flush()

    assert _logs(db_session, kiosk)[0].actor_user_id == actor.id


def test_a_kiosk_with_no_paper_row_gets_one(db_session):
    k = Kiosk(name="Fresh")
    db_session.add(k)
    db_session.flush()

    assert sheets_remaining(db_session, k) == 250


# ── consuming paper after a print ───────────────────────────────────────────


def test_printing_deducts_the_predicted_sheets(db_session, kiosk):
    from app.modules.kiosks.paper import consume_paper

    before = sheets_remaining(db_session, kiosk)  # 50
    consume_paper(db_session, kiosk, predicted_sheets=10)
    assert sheets_remaining(db_session, kiosk) == before - 10


def test_the_devices_actual_count_wins_over_the_prediction(db_session, kiosk):
    """The tray is emptied by what the printer pulled, not what we guessed."""
    from app.modules.kiosks.paper import consume_paper

    before = sheets_remaining(db_session, kiosk)
    consume_paper(db_session, kiosk, predicted_sheets=10, actual_sheets=13)
    assert sheets_remaining(db_session, kiosk) == before - 13


def test_a_failed_print_still_deducts_what_it_used(db_session, kiosk):
    """The old backend deducted zero unless the job reached PRINTED, so a jam
    after three sheets left the counter lying about the tray."""
    from app.modules.kiosks.paper import consume_paper

    before = sheets_remaining(db_session, kiosk)
    consume_paper(db_session, kiosk, predicted_sheets=10, actual_sheets=3)
    assert sheets_remaining(db_session, kiosk) == before - 3


def test_a_mismatch_is_recorded_not_absorbed(db_session, kiosk):
    """A printer that consistently overshoots is reporting a real problem --
    usually a driver ignoring duplex."""
    from app.modules.kiosks.paper import consume_paper

    consume_paper(db_session, kiosk, predicted_sheets=10, actual_sheets=20)
    db_session.flush()

    note = _logs(db_session, kiosk)[-1].note
    assert "20" in note and "expected 10" in note


def test_an_estimate_says_so_in_the_log(db_session, kiosk):
    from app.modules.kiosks.paper import consume_paper

    consume_paper(db_session, kiosk, predicted_sheets=4)
    db_session.flush()
    assert "estimated" in _logs(db_session, kiosk)[-1].note


def test_paper_never_goes_below_empty(db_session, kiosk):
    """The counter models a physical tray, and a tray cannot hold minus four.

    Asserts on `used` rather than `sheets_remaining`: the latter already clamps
    with max(0, ...), so it reports 0 whether or not `used` was clamped -- and a
    `used` above capacity silently breaks the next partial refill, which
    computes from capacity minus used.
    """
    from app.modules.kiosks.paper import consume_paper

    consume_paper(db_session, kiosk, predicted_sheets=9999)
    db_session.flush()

    paper = db_session.get(KioskPaper, kiosk.id)
    assert paper.used == paper.capacity
    assert sheets_remaining(db_session, kiosk) == 0


def test_an_overconsumed_tray_still_refills_correctly(db_session, kiosk):
    """The consequence of the clamp above: if `used` were allowed past capacity,
    setting the tray afterwards would compute from a nonsense baseline."""
    from app.modules.kiosks.paper import consume_paper

    consume_paper(db_session, kiosk, predicted_sheets=9999)
    db_session.flush()

    set_paper(db_session, kiosk, sheets_left=100, actor_user_id=None)
    assert sheets_remaining(db_session, kiosk) == 100


def test_negative_consumption_is_refused(db_session, kiosk):
    from app.modules.kiosks.paper import consume_paper

    with pytest.raises(BadRequest):
        consume_paper(db_session, kiosk, predicted_sheets=-1)


def test_every_print_is_logged(db_session, kiosk):
    from app.modules.kiosks.paper import consume_paper

    before = len(_logs(db_session, kiosk))
    consume_paper(db_session, kiosk, predicted_sheets=2, reference="tsk_abc")
    db_session.flush()

    logs = _logs(db_session, kiosk)
    assert len(logs) == before + 1
    assert "tsk_abc" in logs[-1].note


def test_a_kiosk_without_enough_paper_cannot_take_the_job(db_session, kiosk):
    """Checked before the order is accepted, so nobody is charged for a job the
    kiosk cannot finish."""
    from app.modules.kiosks.paper import can_print

    assert can_print(db_session, kiosk, sheets_needed=50) is True
    assert can_print(db_session, kiosk, sheets_needed=51) is False
