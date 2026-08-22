"""What needs attention, and what was done.

Both tables this router reads existed in the backend being replaced, and neither
was in the path. `notifications` was admin-scoped by an `is_admin_only` flag that
one code path set and nothing filtered on; `admin_audit_log` was written by 15 of
94 mutating routes and read by no route at all. Writing them was never the gap.
This is the gap.

The audit trail resolves actors to people in one batched query. One query per row
is what made the old backend's admin listings slow enough that nobody opened
them, which has the same end state as not having built them.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.api.schemas import (
    AlertResponse,
    AuditEntryResponse,
    PlatformRevenueResponse,
    RevenueBucketResponse,
)
from app.modules.identity import User
from app.modules.identity import repository as identity_repo
from app.modules.identity.roles import Role
from app.modules.ops import AlertSeverity, entries_for, open_alerts, resolve
from app.modules.payments import Earnings, platform_revenue

router = APIRouter(prefix="/v1/admin", tags=["admin"])

CurrentAdmin = Annotated[User, Depends(require_role(Role.ADMIN))]

# A page of a worklist. Large enough that an operator rarely pages, small enough
# that the response stays a response.
MAX_LIMIT = 200


def _as_alert(alert) -> AlertResponse:
    return AlertResponse(
        id=alert.public_id,
        kind=alert.kind,
        severity=AlertSeverity(alert.severity).value,
        summary=alert.summary,
        detail=alert.detail,
        entity_type=alert.entity_type,
        entity_id=alert.entity_id,
        occurrences=alert.occurrences,
        resolved=alert.resolved,
        first_seen_at=alert.created_at,
        last_seen_at=alert.last_seen_at,
    )


@router.get("/alerts", response_model=list[AlertResponse])
def alerts(
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    severity: AlertSeverity | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
) -> list[AlertResponse]:
    """Everything still open, most severe first, then most recently seen.

    `severity` is the enum rather than a string, so an operator who filters for
    something that is not a severity gets a 422. Accepting it and returning an
    unfiltered list would read as "nothing is critical", which is the worst
    possible answer to that question.
    """
    return [
        _as_alert(alert)
        for alert in open_alerts(db, severity=severity, limit=limit)
    ]


@router.post("/alerts/{alert_id}/resolve", response_model=AlertResponse)
def resolve_alert(
    alert_id: str,
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
) -> AlertResponse:
    """Close an alert.

    Resolving one that is already closed returns it unchanged rather than
    refusing: two operators working the same list at once is ordinary, and a
    409 would tell the second one that something is wrong when nothing is.

    Not audited, and that is a decision rather than an omission -- the alert row
    itself records who resolved it and when, which is a durable record of the
    same fact. An audit entry beside it would be a second, weaker copy.
    """
    return _as_alert(resolve(db, public_id=alert_id, actor_user_id=admin.id))


@router.get("/audit", response_model=list[AuditEntryResponse])
def audit_trail(
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    entity_type: str | None = None,
    entity_id: str | None = None,
    action: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 100,
) -> list[AuditEntryResponse]:
    """What was done, newest first, narrowed by whatever the admin knows.

    The filters are the questions this exists to answer: everything that has
    happened to one kiosk, or to one owner's payment configuration, or every
    instance of one action across the estate.
    """
    entries = entries_for(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        limit=limit,
    )

    # One query for the whole page. Ops names its actor by primary key because
    # it may not import identity -- resolving that to a person is the api
    # layer's job, and doing it per row would be the legacy N+1 all over again.
    actors = identity_repo.actors_by_id(
        db, {entry.actor_user_id for entry in entries if entry.actor_user_id}
    )

    return [
        AuditEntryResponse(
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            actor_id=actor.public_id if (actor := actors.get(entry.actor_user_id)) else None,
            actor_email=actor.email if actor else None,
            before=entry.before,
            after=entry.after,
            note=entry.note,
            created_at=entry.created_at,
        )
        for entry in entries
    ]


# ── what the platform took ──────────────────────────────────────────────────


def _bucket(earnings: Earnings) -> RevenueBucketResponse:
    return RevenueBucketResponse(
        gross_inr=earnings.gross_inr,
        refunded_inr=earnings.refunded_inr,
        net_inr=earnings.net_inr,
        payment_count=earnings.order_count,
    )


@router.get("/revenue", response_model=PlatformRevenueResponse)
def revenue(
    admin: CurrentAdmin,
    db: Annotated[Session, Depends(get_db)],
    since: datetime | None = None,
    until: datetime | None = None,
) -> PlatformRevenueResponse:
    """Money in, over a window, in buckets that are not addable.

    `since` and `until` are typed as datetimes rather than parsed here, so a
    request for "lastweek" is a 422 instead of a silently unfiltered answer --
    an operator who asked for this month and got all time would read it as a
    very good month.

    The same settled-payment predicate as an owner's own earnings page. One
    definition, so a platform figure and a shop's figure cannot disagree the way
    the old backend's three copies did.
    """
    figures = platform_revenue(db, since=since, until=until)

    return PlatformRevenueResponse(
        since=since,
        until=until,
        print_platform=_bucket(figures.print_platform),
        print_owners=_bucket(figures.print_owners),
        subscriptions=_bucket(figures.subscriptions),
        wallet_topups=_bucket(figures.wallet_topups),
    )
