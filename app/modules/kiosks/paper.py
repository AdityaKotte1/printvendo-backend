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
) -> None:
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
    )
    db.add(paper)
    return new_left


def mark_out_of_paper(db: Session, kiosk: Kiosk, *, actor_user_id: int | None) -> int:
    """The tray is empty -- reported by the device or by a person."""
    return set_paper(
        db, kiosk, sheets_left=0, actor_user_id=actor_user_id, note="reported empty"
    )
