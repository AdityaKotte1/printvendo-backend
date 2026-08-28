"""Paper: how much is in the tray, and who changed it.

Two rules that are not preferences:

* **Paper is sheets, never a percentage.** Nobody refills a percentage. Both the
  tray size and the amount in it are editable, because a tray is not always 250
  sheets and a refiller rarely fills it to the top.
* **Every change writes a log row**, whoever made it. Otherwise "who let this
  kiosk run dry" has no answer.

Storage is sheets *used* against a capacity, because that is what the machine
reports; every function here speaks in sheets *remaining*, because that is what
people mean.
"""

from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.modules.kiosks.models import (
    DEFAULT_PAPER_CAPACITY,
    Kiosk,
    KioskPaper,
    PaperRefillLog,
)
from app.modules.ops import audit


def _paper(db: Session, kiosk: Kiosk) -> KioskPaper:
    paper = db.get(KioskPaper, kiosk.id)
    if paper is None:
        paper = KioskPaper(kiosk_id=kiosk.id, capacity=DEFAULT_PAPER_CAPACITY, used=0)
        db.add(paper)
        db.flush()
    return paper


def sheets_remaining(db: Session, kiosk: Kiosk) -> int:
    paper = _paper(db, kiosk)
    return max(0, paper.capacity - paper.used)


def tray_capacity(db: Session, kiosk: Kiosk) -> int:
    """How many sheets this tray holds when full.

    Exposed because a bar drawn without it is a bar drawn against a guess: the
    student app assumed a 500-sheet ream, so a 200-sheet tray that had just been
    filled rendered as 40% and read as more than half gone.
    """
    return _paper(db, kiosk).capacity


def _clear_warning_throttle(paper: KioskPaper) -> None:
    """Re-arm the low-paper alerts.

    Without this the next low cycle stays silent, because the throttle still
    believes it has already warned -- so refilling would quietly disable the
    warnings for that kiosk.
    """
    paper.warning_count = 0
    paper.last_warning_at = None
    paper.last_reminder_at = None


def _log(
    db: Session,
    kiosk: Kiosk,
    paper: KioskPaper,
    *,
    sheets_added: int,
    used_before: int,
    actor_user_id: int | None,
    note: str | None,
    action: str,
) -> None:
    """The one place a paper change is written down, twice over.

    `PaperRefillLog` is the operational record -- what a refiller sees as this
    kiosk's history. The audit entry is the accountability record, in the same
    table as every other change somebody made to a business they may not own.

    Auditing here rather than in each route is deliberate: reset, set and
    out-of-paper all pass through this function, and each is reachable from both
    the owner and the refiller router. Wiring it per route would be six call
    sites for one rule, which is how the old backend's audit ended up covering
    16% of what it should have.
    """
    db.add(
        PaperRefillLog(
            kiosk_id=kiosk.id,
            actor_user_id=actor_user_id,
            sheets_added=sheets_added,
            capacity_at_change=paper.capacity,
            used_before_change=used_before,
            note=note,
        )
    )

    audit.record(
        db,
        action=action,
        entity_type="kiosk",
        entity_id=kiosk.public_id,
        actor_user_id=actor_user_id,
        before={"used": used_before, "capacity": paper.capacity},
        after={"used": paper.used, "capacity": paper.capacity},
        note=note,
    )


def reset_paper(db: Session, kiosk: Kiosk, *, actor_user_id: int | None) -> int:
    """Refill to a full tray. Returns sheets remaining."""
    paper = _paper(db, kiosk)
    used_before = paper.used

    paper.used = 0
    _clear_warning_throttle(paper)

    _log(
        db,
        kiosk,
        paper,
        sheets_added=used_before,
        used_before=used_before,
        actor_user_id=actor_user_id,
        note="reset to full",
        action="kiosk.paper.reset",
    )
    db.add(paper)
    return paper.capacity


def set_paper(
    db: Session,
    kiosk: Kiosk,
    *,
    capacity: int | None = None,
    sheets_left: int | None = None,
    actor_user_id: int | None = None,
    note: str | None = None,
) -> int:
    """Set tray size, amount in it, or both. Returns sheets remaining.

    Changing capacity alone preserves sheets remaining -- resizing a tray does
    not add or remove paper.
    """
    if capacity is None and sheets_left is None:
        raise BadRequest("Say how big the tray is, how much paper is in it, or both.")

    paper = _paper(db, kiosk)
    used_before = paper.used
    old_left = max(0, paper.capacity - paper.used)

    if capacity is not None:
        if capacity < 1:
            raise BadRequest("A paper tray holds at least one sheet.")
        paper.capacity = capacity

    new_left = old_left if sheets_left is None else sheets_left
    if new_left < 0:
        raise BadRequest("A tray cannot hold a negative number of sheets.")
    if new_left > paper.capacity:
        raise BadRequest(
            f"That tray holds {paper.capacity} sheets, so it cannot contain {new_left}."
        )

    paper.used = paper.capacity - new_left

    if new_left > old_left:
        _clear_warning_throttle(paper)

    _log(
        db,
        kiosk,
        paper,
        sheets_added=max(0, new_left - old_left),
        used_before=used_before,
        actor_user_id=actor_user_id,
        note=note,
        action="kiosk.paper.set",
    )
    db.add(paper)
    return new_left


def mark_out_of_paper(db: Session, kiosk: Kiosk, *, actor_user_id: int | None) -> int:
    """The tray is empty -- reported by the device or by a person."""
    return set_paper(
        db, kiosk, sheets_left=0, actor_user_id=actor_user_id, note="reported empty"
    )


def consume_paper(
    db: Session,
    kiosk: Kiosk,
    *,
    predicted_sheets: int,
    actual_sheets: int | None = None,
    reference: str | None = None,
) -> int:
    """Deduct paper after a print, preferring what the device actually used.

    `predicted_sheets` is what workload() expected. `actual_sheets` is what the
    printer reported (CUPS `job-media-sheets-completed`), when it can say.

    Actual wins. The backend being replaced always used its own estimate and
    only deducted on success, so a job that jammed after three sheets deducted
    zero and the counter drifted from the physical tray -- which is how a kiosk
    ends up reporting paper while being empty.

    A difference between the two is recorded rather than absorbed. A printer
    that consistently uses more sheets than predicted is telling you something,
    usually that its driver is ignoring the duplex request.

    Returns sheets remaining.
    """
    used = actual_sheets if actual_sheets is not None else predicted_sheets
    if used < 0:
        raise BadRequest("A print cannot consume a negative number of sheets.")

    paper = _paper(db, kiosk)
    used_before = paper.used

    # Never below zero and never past the tray's size: the counter is a model of
    # a physical tray, and a tray cannot hold minus four sheets.
    paper.used = min(paper.capacity, used_before + used)

    note = f"printed {used} sheet{'s' if used != 1 else ''}"
    if reference:
        note = f"{note} for {reference}"
    if actual_sheets is not None and actual_sheets != predicted_sheets:
        note = f"{note} (expected {predicted_sheets})"
    elif actual_sheets is None:
        note = f"{note} (estimated; device did not report)"

    _log(
        db,
        kiosk,
        paper,
        sheets_added=-used,
        used_before=used_before,
        actor_user_id=None,
        # Printing consumes paper; that is the machine, not a person. Recorded
        # with no actor rather than omitted, so "the tray emptied and nobody
        # touched it" is a statement the trail can actually make.
        note=note,
        action="kiosk.paper.consumed",
    )
    db.add(paper)
    return max(0, paper.capacity - paper.used)


def can_print(db: Session, kiosk: Kiosk, *, sheets_needed: int) -> bool:
    """Whether this kiosk has enough paper for a job of this size.

    Checked before an order is accepted, so a student is not charged for a job
    the kiosk cannot finish.
    """
    return sheets_remaining(db, kiosk) >= sheets_needed
