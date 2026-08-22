"""The four things that run on a schedule.

Each takes a session and settings, does one sweep, and returns a sentence for
the log. They are functions rather than a class because the scheduler owns the
timing, the lock and the transaction: a job's only job is the sweep.

This module is a composition root, like `app/api`. It is allowed to know about
several bounded contexts at once -- the paper watcher reads kiosks and writes an
ops alert -- because that combination is what a sweep is. What it must not do is
reach into another module's tables, and the import contracts say so.

**The watchers stand down.** A condition a machine noticed and a machine can see
the end of is resolved by the same sweep that raised it. Without that, a kiosk
that was offline for ten minutes leaves a row nobody will ever close, and the
console fills with problems that stopped being problems -- which is the wall of
unread notifications this system exists to avoid, reached by a different road.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.modules.kiosks import (
    HEARTBEAT_WINDOW,
    Kiosk,
    OnboardingStage,
    device_of,
    is_online,
    sheets_remaining,
    system_scope,
)
from app.modules.kiosks import repository as kiosk_repo
from app.modules.ops import AlertSeverity, raise_alert, resolve_by_key
from app.modules.orders import expire_stale_orders
from app.modules.printing import DocumentStore, purge_expired_files

logger = logging.getLogger(__name__)

# A tray this low is worth a journey. Sheets rather than a percentage, because
# nobody refills a percentage -- and because a 10% warning on a 50-sheet tray is
# five sheets, which is no warning at all.
LOW_PAPER_SHEETS = 50

# A shop that has been dark this long is not a network blip.
OFFLINE_IS_SERIOUS_AFTER = timedelta(hours=1)


def expire_orders(db: Session, settings: Settings) -> str:
    """Close orders nobody paid for, releasing the paper they were holding."""
    expired = expire_stale_orders(db)
    return f"expired {len(expired)} unpaid orders" if expired else ""


def purge_files(db: Session, settings: Settings) -> str:
    """Delete the files of documents nothing is waiting on any more.

    The row survives, marked EXPIRED, so an order history keeps its shape.
    """
    purged = purge_expired_files(
        db,
        DocumentStore(settings.STORAGE_ROOT),
        older_than=timedelta(days=settings.FILE_RETENTION_DAYS),
    )
    return f"purged the files of {len(purged)} documents" if purged else ""


def watch_offline_kiosks(db: Session, settings: Settings) -> str:
    """Report shops that cannot currently be sent work.

    Derived from the last heartbeat, never from the device's status column: a Pi
    whose power was pulled does not get to send "I am going offline", which is
    why the old backend's status stayed ONLINE until a person noticed.
    """
    now = datetime.now(UTC)
    offline = 0

    for kiosk in _selling_kiosks(db):
        key = f"kiosk.offline:{kiosk.public_id}"
        device = device_of(db, kiosk)

        if device is not None and is_online(device, now=now):
            resolve_by_key(db, dedupe_key=key, now=now)
            continue

        offline += 1
        raise_alert(
            db,
            kind="kiosk.offline",
            severity=_offline_severity(device, now),
            summary=f"{kiosk.name} is not reachable and cannot print.",
            dedupe_key=key,
            entity_type="kiosk",
            entity_id=kiosk.public_id,
            detail=_offline_detail(device, now),
            now=now,
        )

    return f"{offline} kiosks are offline" if offline else ""


def watch_paper(db: Session, settings: Settings) -> str:
    """Report trays that are empty or nearly so.

    One alert per kiosk whether it is low or empty, deliberately: a tray that
    runs out after being low is the same problem getting worse, and `raise_alert`
    escalates the open row rather than opening a second one. Two rows would make
    "how long has this shop been out of paper" a question with two answers.
    """
    now = datetime.now(UTC)
    empty = low = 0

    for kiosk in _selling_kiosks(db):
        key = f"kiosk.paper:{kiosk.public_id}"
        remaining = sheets_remaining(db, kiosk)

        if remaining > LOW_PAPER_SHEETS:
            resolve_by_key(db, dedupe_key=key, now=now)
            continue

        if remaining == 0:
            empty += 1
            severity = AlertSeverity.CRITICAL
            summary = f"{kiosk.name} is out of paper."
        else:
            low += 1
            severity = AlertSeverity.WARNING
            summary = f"{kiosk.name} has {remaining} sheets left."

        raise_alert(
            db,
            kind="kiosk.paper.low",
            severity=severity,
            summary=summary,
            dedupe_key=key,
            entity_type="kiosk",
            entity_id=kiosk.public_id,
            detail={"sheets_remaining": remaining},
            now=now,
        )

    if not (empty or low):
        return ""
    return f"{empty} kiosks out of paper, {low} running low"


def _selling_kiosks(db: Session) -> list[Kiosk]:
    """The shops a student could be sent to right now.

    A sweep has no actor, so it reads through `system_scope` -- the one named
    way to say "this is not being done on anybody's behalf". Anything short of
    LIVE is somebody's half-finished setup, and alerting on a kiosk that nobody
    expects to work yet is how a console becomes noise.
    """
    return [
        kiosk
        for kiosk in kiosk_repo.list_kiosks(db, system_scope())
        if kiosk.onboarding_stage == OnboardingStage.LIVE
    ]


def _offline_severity(device, now: datetime) -> AlertSeverity:
    if device is None or device.last_heartbeat_at is None:
        # Never heard from at all, at a kiosk that is supposed to be selling.
        return AlertSeverity.CRITICAL
    dark_for = now - device.last_heartbeat_at
    return (
        AlertSeverity.CRITICAL
        if dark_for >= OFFLINE_IS_SERIOUS_AFTER
        else AlertSeverity.WARNING
    )


def _offline_detail(device, now: datetime) -> dict:
    if device is None:
        return {"reason": "no device is enrolled at this kiosk"}
    if device.last_heartbeat_at is None:
        return {"reason": "this device has never reported"}
    return {
        "last_seen_at": device.last_heartbeat_at.isoformat(),
        "minutes_dark": int((now - device.last_heartbeat_at).total_seconds() // 60),
        "window_minutes": int(HEARTBEAT_WINDOW.total_seconds() // 60),
    }
