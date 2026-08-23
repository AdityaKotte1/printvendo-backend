"""Paper for the refiller app.

A refiller services machines. They need to know where a kiosk is and how much
paper is in it, and nothing else -- not what it charges, not what it earns, not
who printed what.

That is enforced by the response type rather than by remembering to strip
fields: `RefillerKioskResponse` has no price or earnings field to populate, so
there is nothing to leak. The old backend kept a separate refiller router that
mirrored the owner one, which is a shape that invites a field being copied
across.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, KioskScope, get_db, require_any_role
from app.api.schemas import (
    PaperResponse,
    PaperUpdateRequest,
    RefillerKioskResponse,
    RefillLogResponse,
)
from app.modules.identity.roles import Role
from app.modules.kiosks import Kiosk
from app.modules.kiosks import repository as kiosk_repo
from app.modules.kiosks.paper import (
    mark_out_of_paper,
    reset_paper,
    set_paper,
    sheets_remaining,
)

router = APIRouter(
    prefix="/v1/refiller/kiosks",
    tags=["refiller"],
    # A refiller, or an admin standing in for one. Scope still decides which
    # kiosks are behind the door.
    dependencies=[Depends(require_any_role(Role.REFILLER, Role.ADMIN))],
)


def _paper_of(db: Session, kiosk: Kiosk) -> PaperResponse:
    return PaperResponse(
        capacity=(kiosk.paper.capacity if kiosk.paper else 250),
        sheets_remaining=sheets_remaining(db, kiosk),
    )


def _as_response(db: Session, kiosk: Kiosk) -> RefillerKioskResponse:
    return RefillerKioskResponse(
        id=kiosk.public_id,
        name=kiosk.name,
        location_description=kiosk.location_description,
        is_active=kiosk.is_active,
        paper=_paper_of(db, kiosk),
    )


@router.get("", response_model=list[RefillerKioskResponse])
def my_rounds(
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> list[RefillerKioskResponse]:
    """The kiosks this refiller covers.

    Scoped by assignment, so covering one shop of an owner who has five does
    not reveal the other four.
    """
    return [_as_response(db, k) for k in kiosk_repo.list_kiosks(db, scope)]


@router.get("/{kiosk_id}", response_model=RefillerKioskResponse)
def kiosk_detail(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> RefillerKioskResponse:
    return _as_response(db, kiosk_repo.get_kiosk(db, scope, kiosk_id))


@router.get("/{kiosk_id}/paper", response_model=PaperResponse)
def get_paper(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> PaperResponse:
    return _paper_of(db, kiosk_repo.get_kiosk(db, scope, kiosk_id))


@router.put("/{kiosk_id}/paper", response_model=PaperResponse)
def update_paper(
    kiosk_id: str,
    payload: PaperUpdateRequest,
    user: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> PaperResponse:
    """Say how much paper is actually in the tray.

    Both the tray size and the amount in it are editable, because a tray is not
    always 250 sheets and a refiller rarely fills it to the top.
    """
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    set_paper(
        db,
        kiosk,
        capacity=payload.capacity,
        sheets_left=payload.sheets_left,
        actor_user_id=user.id,
        note=payload.note,
    )
    return _paper_of(db, kiosk)


@router.post("/{kiosk_id}/paper/reset", response_model=PaperResponse)
def refill(
    kiosk_id: str,
    user: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> PaperResponse:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    reset_paper(db, kiosk, actor_user_id=user.id)
    return _paper_of(db, kiosk)


@router.post("/{kiosk_id}/paper/out-of-paper", response_model=PaperResponse)
def report_empty(
    kiosk_id: str,
    user: CurrentUser,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
) -> PaperResponse:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    mark_out_of_paper(db, kiosk, actor_user_id=user.id)
    return _paper_of(db, kiosk)


@router.get("/{kiosk_id}/refill-logs", response_model=list[RefillLogResponse])
def refill_logs(
    kiosk_id: str,
    scope: KioskScope,
    db: Annotated[Session, Depends(get_db)],
    limit: int = 50,
) -> list[RefillLogResponse]:
    kiosk = kiosk_repo.get_kiosk(db, scope, kiosk_id)
    return [
        RefillLogResponse(
            sheets_added=row.sheets_added,
            capacity_at_change=row.capacity_at_change,
            used_before_change=row.used_before_change,
            note=row.note,
            created_at=row.created_at,
        )
        for row in kiosk_repo.list_refill_logs(db, kiosk, limit=limit)
    ]
