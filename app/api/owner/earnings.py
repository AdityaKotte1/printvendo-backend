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

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import KioskScope, get_db, require_role
from app.api.schemas import (
    EarningsResponse,
    KioskEarningsResponse,
    OwnerOrderResponse,
)
from app.modules.identity import User
from app.modules.identity.roles import Role
from app.modules.kiosks import repository as kiosk_repo
from app.modules.orders import orders_at_kiosks
from app.modules.payments import Earnings, earnings_by_kiosk, earnings_for_kiosks

router = APIRouter(prefix="/v1/owner", tags=["owner"])

CurrentOwner = Annotated[User, Depends(require_role(Role.OWNER))]


def _as_earnings(earnings: Earnings) -> EarningsResponse:
    return EarningsResponse(
        gross_inr=earnings.gross_inr,
        refunded_inr=earnings.refunded_inr,
        net_inr=earnings.net_inr,
        order_count=earnings.order_count,
    )


@router.get("/earnings", response_model=EarningsResponse)
def my_earnings(
    owner: CurrentOwner,
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
    owner: CurrentOwner,
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


@router.get("/kiosks/{kiosk_id}/orders", response_model=list[OwnerOrderResponse])
def kiosk_orders(
    kiosk_id: str,
    owner: CurrentOwner,
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
