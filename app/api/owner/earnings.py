"""What an owner's shops took, and what was printed there.

Two rules carry this router.

**No student is identifiable here.** `OwnerOrderResponse` has no name, no email
and no user id -- not stripped, absent. A shop owner has a legitimate interest
in what was printed and what it cost, and none at all in who printed it. A type
with no field for a person cannot leak one however the handler is later edited.

**Scope comes from the resolver, never from a query parameter.** Every read
starts from `kiosk_scope`, so an owner asking about a kiosk id they do not hold
gets the same answer as one asking about a kiosk that never existed. The old
backend's `/owner/*` was a second router with a "DO NOT LOOSEN" comment where a
check should have been.
"""

import csv
import io
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    CurrentUser,
    KioskScope,
    get_db,
    require_any_role,
)
from app.api.schemas import (
    DayEarningsResponse,
    EarningsResponse,
    KioskEarningsResponse,
    OwnerOrderResponse,
)
from app.core.errors import BadRequest
from app.modules.identity.roles import Role
from app.modules.kiosks import repository as kiosk_repo
from app.modules.orders import orders_at_kiosks, paid_orders_at_kiosks
from app.modules.payments import (
    Earnings,
    earnings_by_day,
    earnings_by_kiosk,
    earnings_for_kiosks,
)

router = APIRouter(
    prefix="/v1/owner",
    tags=["owner"],
    dependencies=[Depends(require_any_role(Role.OWNER, Role.ADMIN))],
)

# An export is a whole-file download built in memory, so it needs a ceiling. Ten
# thousand rows is several years of a busy shop and a few megabytes of CSV.
MAX_EXPORT_ROWS = 10_000

TOO_MANY_ROWS = (
    "That period has more orders than one export can hold. "
    "Please choose a shorter period."
)

# No filename, no student, no free text of any kind: an owner has a legitimate
# interest in what was printed and what it cost, and none at all in who printed
# it. "Medical Results Ravi Kumar.pdf" names a person as surely as an address
# does. Every column here is an id, a date, an enum or a number, which is also
# why no cell can begin with `=` and be run as a formula by a spreadsheet.
EXPORT_COLUMNS = (
    "order_id",
    "paid_at",
    "state",
    "payment_method",
    "documents",
    "sheets",
    "subtotal_inr",
    "fee_inr",
    "total_inr",
    "refunded_at",
)

# Not `require_role(Role.OWNER)`. The router above already refuses anyone who
# is neither an owner nor an admin, and narrowing again here refused the admin
# the matrix promises -- which is how these four routes came to 403 an admin
# while `tests/authz/matrix.py` said {OWNER, ADMIN}. `kiosk_scope` is what
# decides which shops are behind the door, and for an admin that is all of
# them. Every other owner router already does it this way.


def _as_earnings(earnings: Earnings) -> EarningsResponse:
    return EarningsResponse(
        gross_inr=earnings.gross_inr,
        refunded_inr=earnings.refunded_inr,
        net_inr=earnings.net_inr,
        order_count=earnings.order_count,
    )


@router.get("/earnings", response_model=EarningsResponse)
def my_earnings(
    owner: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    since: datetime | None = None,
    until: datetime | None = None,
) -> EarningsResponse:
    """Everything this owner's kiosks took over the window.

    `net_inr` may be negative -- refunds issued today against takings from last
    week -- and is reported as such. The old backend clamped it to zero, which
    hid the one case an operator actually has to act on.
    """
    kiosks = kiosk_repo.list_kiosks(db, scope, include_inactive=True)
    return _as_earnings(
        earnings_for_kiosks(db, [k.id for k in kiosks], since=since, until=until)
    )


@router.get("/earnings/by-kiosk", response_model=list[KioskEarningsResponse])
def my_earnings_by_kiosk(
    owner: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[KioskEarningsResponse]:
    """The same figures split per shop, in one query rather than one per shop.

    A kiosk that took nothing appears with zeros rather than being absent: a
    missing row renders as a blank, and zero is the true and more useful answer.
    """
    kiosks = kiosk_repo.list_kiosks(db, scope, include_inactive=True)
    split = earnings_by_kiosk(db, [k.id for k in kiosks], since=since, until=until)

    return [
        KioskEarningsResponse(
            kiosk_id=kiosk.public_id,
            kiosk_name=kiosk.name,
            earnings=_as_earnings(split[kiosk.id]),
        )
        for kiosk in kiosks
    ]


@router.get("/earnings/daily", response_model=list[DayEarningsResponse])
def my_earnings_by_day(
    owner: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    kiosk_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[DayEarningsResponse]:
    """The same window, day by day, for a chart.

    A real endpoint rather than a series each console adds up for itself. The
    admin console built one in the browser out of the order export -- which
    caps at a row count, buckets in UTC and cannot see a refund -- and the owner
    app was about to build a second. Two implementations of "what did this shop
    take on Tuesday" is the shape of every defect in the legacy audit, so there
    is one, and it is the arithmetic that already produces the total.

    `kiosk_id` narrows it to one shop, through the same resolver as everything
    else here: a kiosk the caller does not hold is 404, byte-identical to one
    that never existed.
    """
    if kiosk_id is not None:
        kiosk_ids = [kiosk_repo.get_kiosk(db, scope, kiosk_id).id]
    else:
        kiosk_ids = [
            kiosk.id for kiosk in kiosk_repo.list_kiosks(db, scope, include_inactive=True)
        ]

    return [
        DayEarningsResponse(day=row.day, earnings=_as_earnings(row.earnings))
        for row in earnings_by_day(db, kiosk_ids, since=since, until=until)
    ]


@router.get("/kiosks/{kiosk_id}/orders", response_model=list[OwnerOrderResponse])
def kiosk_orders(
    kiosk_id: str,
    owner: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(le=200)] = 50,
) -> list[OwnerOrderResponse]:
    """What was printed at this shop, newest first.

    Goes through `get_kiosk` with the scope, so a kiosk the owner does not hold
    is 404 -- byte-identical to one that never existed. A 403 would confirm the
    kiosk exists, which tells one shop owner something true about a competitor's
    estate.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)

    return [
        OwnerOrderResponse(
            id=view.id,
            state=view.state.value,
            payment_method=view.payment_method,
            total_inr=view.total_inr,
            sheets=sum(item.sheets for item in view.items),
            document_count=len(view.items),
            paid_at=view.paid_at,
            refunded_at=view.refunded_at,
            created_at=view.created_at,
        )
        for view in orders_at_kiosks(db, kiosk_ids=[kiosk.id], limit=limit)
    ]


@router.get("/kiosks/{kiosk_id}/orders/export")
def export_kiosk_orders(
    kiosk_id: str,
    owner: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    since: datetime | None = None,
    until: datetime | None = None,
) -> Response:
    """This shop's takings over a period, as a file for a spreadsheet.

    Paid orders only, windowed on when the money was taken, because this has to
    reconcile with `/v1/owner/earnings` above it. An export whose total
    disagrees with the figure on the page is worse than no export.

    **Refused rather than truncated** past `MAX_EXPORT_ROWS`. A silently short
    CSV is a wrong number that looks exactly like a right one, and the person
    reading it is doing accounts.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)

    # One more than the cap: enough to know the range is too big without
    # counting the whole thing first.
    views = paid_orders_at_kiosks(
        db,
        kiosk_ids=[kiosk.id],
        since=since,
        until=until,
        limit=MAX_EXPORT_ROWS + 1,
    )
    if len(views) > MAX_EXPORT_ROWS:
        raise BadRequest(TOO_MANY_ROWS)

    buffer = io.StringIO()
    # Excel on Windows needs CRLF, and `csv` writes it only if asked. The
    # StringIO is newline-agnostic, so this is the one place it is decided.
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for view in views:
        writer.writerow(
            (
                view.id,
                view.paid_at.isoformat() if view.paid_at else "",
                view.state.value,
                view.payment_method or "",
                len(view.items),
                sum(item.sheets for item in view.items),
                view.subtotal_inr,
                view.fee_inr,
                view.total_inr,
                view.refunded_at.isoformat() if view.refunded_at else "",
            )
        )

    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            # Named by the opaque id, never by the shop's name. A name is
            # somebody else's text and a header is a line-based format: one
            # carrying a quote or a newline would rewrite the response headers.
            "Content-Disposition": (
                f'attachment; filename="{kiosk.public_id}-orders.csv"'
            )
        },
    )
