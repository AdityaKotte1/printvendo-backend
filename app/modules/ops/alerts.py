"""Raising something for an operator to look at.

The rule that makes this usable rather than noise: **the same unresolved problem
is one row, with a count.** A kiosk offline for a week is one alert seen 10,080
times, not 10,080 alerts. The old backend's notifications table had no such
notion, so the console showed a wall of identical rows and people stopped
reading it -- an alert nobody reads is worse than no alert, because it looks
like coverage.

Deduplication is scoped to *unresolved* alerts only. The same condition
recurring after somebody fixed it raises a fresh alert rather than quietly
bumping a closed one, because "this came back" is exactly the thing an operator
needs to see.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.modules.ops.audit import scrub
from app.modules.ops.models import AdminAlert, AlertSeverity

NO_SUCH_ALERT = "That alert does not exist."


def raise_alert(
    db: Session,
    *,
    kind: str,
    severity: AlertSeverity,
    summary: str,
    dedupe_key: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict | None = None,
    now: datetime | None = None,
) -> AdminAlert:
    """Report a condition, creating the alert or bumping the open one.

    Returns the row either way, so a caller cannot tell -- and does not need to
    -- whether this was the first occurrence. Nothing about the reporting site
    should depend on that.
    """
    now = now or datetime.now(UTC)

    existing = db.execute(
        select(AdminAlert).where(
            AdminAlert.dedupe_key == dedupe_key,
            AdminAlert.resolved.is_(False),
        )
    ).scalars().first()

    if existing is not None:
        existing.occurrences += 1
        existing.last_seen_at = now
        # The newest detail wins: "offline since 09:12" is more use than the
        # first report's wording, and the count already carries the history.
        if detail is not None:
            existing.detail = scrub(detail)
        # A condition that has got worse should read as worse. It never
        # de-escalates on its own -- an operator resolves it instead.
        if _rank(severity) > _rank(existing.severity):
            existing.severity = severity
        db.add(existing)
        db.flush()
        return existing

    alert = AdminAlert(
        kind=kind,
        severity=severity,
        summary=summary[:300],
        detail=scrub(detail) if detail is not None else None,
        entity_type=entity_type,
        entity_id=entity_id,
        dedupe_key=dedupe_key,
        last_seen_at=now,
    )
    db.add(alert)
    try:
        db.flush()
    except IntegrityError:
        # Two reporters raced past the SELECT. The partial unique index decides;
        # roll back to before the insert and take the row that won.
        db.rollback()
        return raise_alert(
            db,
            kind=kind,
            severity=severity,
            summary=summary,
            dedupe_key=dedupe_key,
            entity_type=entity_type,
            entity_id=entity_id,
            detail=detail,
            now=now,
        )
    return alert


_ORDER = {
    AlertSeverity.INFO: 0,
    AlertSeverity.WARNING: 1,
    AlertSeverity.CRITICAL: 2,
}


def _rank(severity: AlertSeverity) -> int:
    return _ORDER[AlertSeverity(severity)]


def open_alerts(
    db: Session, *, severity: AlertSeverity | None = None, limit: int = 100
) -> list[AdminAlert]:
    """What still needs attention, most severe first, then most recent."""
    stmt = select(AdminAlert).where(AdminAlert.resolved.is_(False))
    if severity is not None:
        stmt = stmt.where(AdminAlert.severity == severity)

    alerts = list(db.execute(stmt.limit(limit)).scalars())
    # Sorted in Python: the severity column is text, so ordering by it in SQL
    # would sort alphabetically -- critical, info, warning -- which reads as a
    # priority order and is not one.
    return sorted(alerts, key=lambda a: (-_rank(a.severity), -a.last_seen_at.timestamp()))


def resolve(
    db: Session,
    *,
    public_id: str,
    actor_user_id: int | None,
    now: datetime | None = None,
) -> AdminAlert:
    """Close an alert. Resolving one already closed is a no-op, not an error."""
    alert = db.execute(
        select(AdminAlert).where(AdminAlert.public_id == public_id)
    ).scalar_one_or_none()
    if alert is None:
        raise NotFound(NO_SUCH_ALERT)

    if alert.resolved:
        return alert

    alert.resolved = True
    alert.resolved_at = now or datetime.now(UTC)
    alert.resolved_by_user_id = actor_user_id
    db.add(alert)
    db.flush()
    return alert


def resolve_by_key(
    db: Session, *, dedupe_key: str, now: datetime | None = None
) -> AdminAlert | None:
    """Close the open alert for a condition that has cleared, if there is one.

    For conditions a machine notices and a machine can see the end of -- a kiosk
    that came back, a tray somebody refilled. The detector holds the dedupe key
    and never sees a public id, which is why this is not `resolve` with a
    different lookup.

    Returns None when nothing was open, because that is the ordinary case: a
    sweep runs every few minutes and almost always finds everything well.
    Resolving nothing is not a failure and must not read like one.

    Leaves an already-resolved alert exactly as it is, including whoever closed
    it -- overwriting a person's name with "the system" would lose the one thing
    the row records about how the problem ended.
    """
    alert = db.execute(
        select(AdminAlert).where(
            AdminAlert.dedupe_key == dedupe_key,
            AdminAlert.resolved.is_(False),
        )
    ).scalars().first()
    if alert is None:
        return None

    alert.resolved = True
    alert.resolved_at = now or datetime.now(UTC)
    # No actor: nobody did this. The absence is the record -- "it stopped on its
    # own" is a different fact from "somebody dealt with it".
    alert.resolved_by_user_id = None
    db.add(alert)
    db.flush()
    return alert
