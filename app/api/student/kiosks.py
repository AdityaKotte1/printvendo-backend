"""The shops a student can send a job to.

Deliberately **not** routed through `kiosk_scope`. That resolver answers "which
kiosks may this person manage", and a student manages none -- their scope is
empty, so reusing it here would show them nothing. Which shops a student may
print at is a different question with a different answer: any kiosk that is
live, active, and currently able to take a payment.

The gate is what makes the last part true, and it is applied here rather than in
the kiosks module because the gate lives in payments and kiosks must not import
it. A CLOSED kiosk -- a SOLD shop whose subscription lapsed, or whose owner's
keys are gone -- is not listed and cannot be ordered from. In the backend being
replaced that rule was written down and never put in the path, so a kiosk with
no owner at all quietly collected into the platform's account.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_db
from app.api.schemas import StudentKioskResponse
from app.api.student.visibility import student_may_order
from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.kiosks import (
    Kiosk,
    effective_prices,
    favourite_kiosk,
    favourite_kiosk_ids,
    sheets_remaining,
    tray_capacity,
    unfavourite_kiosk,
)
from app.modules.kiosks import repository as kiosk_repo
from app.modules.kiosks.scope import Scope

router = APIRouter(prefix="/v1/app/kiosks", tags=["student"])

NO_SUCH_KIOSK = "That kiosk does not exist."

# Everything, then filtered. A student is not restricted to an assignment, so
# the scope that expresses "no restriction" is the admin one -- reused rather
# than re-implemented, so there is still exactly one unscoped read in the
# codebase and it is the one the scope resolver already returns.
UNRESTRICTED = Scope(is_unrestricted=True, kiosk_ids=frozenset())


def _printable(db: Session, kiosk: Kiosk) -> bool:
    """Whether a student may order here at all.

    Delegates rather than deciding: the order route needs the same answer, and
    when this file held the rule alone that route did not ask it.
    """
    return student_may_order(db, kiosk)


def _as_response(
    db: Session, kiosk: Kiosk, *, saved: set[int] | None = None
) -> StudentKioskResponse:
    prices = effective_prices(kiosk)
    remaining = sheets_remaining(db, kiosk)
    return StudentKioskResponse(
        id=kiosk.public_id,
        name=kiosk.name,
        latitude=kiosk.latitude,
        longitude=kiosk.longitude,
        location_description=kiosk.location_description,
        accepts_wallet=kiosk.accepts_wallet,
        is_favourite=saved is not None and kiosk.id in saved,
        # Derived from the tray rather than carried as a flag, so it cannot
        # disagree with the number printed next to it.
        is_out_of_paper=remaining <= 0,
        sheets_remaining=remaining,
        paper_capacity=tray_capacity(db, kiosk),
        price_bw_single=prices["bw_single"],
        price_bw_double=prices["bw_double"],
        price_color_single=prices["color_single"],
        price_color_double=prices["color_double"],
    )


@router.get("", response_model=list[StudentKioskResponse])
def list_printable_kiosks(
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> list[StudentKioskResponse]:
    """Every shop currently able to print.

    Out-of-paper kiosks are listed rather than hidden: the tray gets refilled,
    the student can see the state before choosing, and hiding them would make a
    shop vanish from the map for a reason nobody standing in front of it could
    understand.
    """
    kiosks = kiosk_repo.list_kiosks(db, UNRESTRICTED)
    # One query for the whole page rather than one per shop. The set marks up a
    # list that has already been fetched and filtered; it is not a way of
    # reading kiosks, and there is still no unscoped read of those.
    saved = favourite_kiosk_ids(db, user_id=user.id)
    return [_as_response(db, k, saved=saved) for k in kiosks if _printable(db, k)]


@router.get("/{kiosk_id}", response_model=StudentKioskResponse)
def get_printable_kiosk(
    kiosk_id: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> StudentKioskResponse:
    """One shop.

    A kiosk that exists but cannot take payment is 404, byte-identical to one
    that never existed. The alternative tells a student -- and so anyone -- that
    a particular shop's subscription has lapsed, which is a true fact about a
    business that is none of their concern.
    """
    try:
        parse_id(kiosk_id, IdPrefix.KIOSK)
    except InvalidId:
        raise NotFound(NO_SUCH_KIOSK) from None

    kiosk = kiosk_repo.get_kiosk(db, UNRESTRICTED, kiosk_id)
    if not _printable(db, kiosk):
        raise NotFound(NO_SUCH_KIOSK)
    return _as_response(db, kiosk, saved=favourite_kiosk_ids(db, user_id=user.id))


def _savable(db: Session, user, kiosk_id: str) -> Kiosk:
    """The shop this student may save, or the same 404 as anywhere else.

    Saving goes through the same visibility rule as looking: a kiosk a student
    cannot see is a kiosk they cannot save, so the star cannot be used to find
    out whether a shop exists.
    """
    try:
        parse_id(kiosk_id, IdPrefix.KIOSK)
    except InvalidId:
        raise NotFound(NO_SUCH_KIOSK) from None

    kiosk = kiosk_repo.get_kiosk(db, UNRESTRICTED, kiosk_id)
    if not _printable(db, kiosk):
        raise NotFound(NO_SUCH_KIOSK)
    return kiosk


@router.put("/{kiosk_id}/favourite", status_code=status.HTTP_204_NO_CONTENT)
def save_kiosk(
    kiosk_id: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Save a shop. Saving one already saved changes nothing.

    PUT rather than POST because it is a toggle being set to a value, and a
    student tapping a star twice on a slow connection must not be told off.
    """
    favourite_kiosk(db, user_id=user.id, kiosk=_savable(db, user, kiosk_id))


@router.delete("/{kiosk_id}/favourite", status_code=status.HTTP_204_NO_CONTENT)
def forget_kiosk(
    kiosk_id: str,
    user: CurrentUser,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    """Forget a shop. Forgetting one that was never saved is not an error."""
    unfavourite_kiosk(db, user_id=user.id, kiosk=_savable(db, user, kiosk_id))
