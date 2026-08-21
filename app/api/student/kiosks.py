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

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_db
from app.api.schemas import StudentKioskResponse
from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.kiosks import Kiosk, effective_prices, sheets_remaining
from app.modules.kiosks import repository as kiosk_repo
from app.modules.kiosks.enums import OnboardingStage
from app.modules.kiosks.scope import Scope
from app.modules.payments import can_take_payment

router = APIRouter(prefix="/v1/app/kiosks", tags=["student"])

NO_SUCH_KIOSK = "That kiosk does not exist."

# Everything, then filtered. A student is not restricted to an assignment, so
# the scope that expresses "no restriction" is the admin one -- reused rather
# than re-implemented, so there is still exactly one unscoped read in the
# codebase and it is the one the scope resolver already returns.
UNRESTRICTED = Scope(is_unrestricted=True, kiosk_ids=frozenset())


def _printable(db: Session, kiosk: Kiosk) -> bool:
    """Whether a student may order here at all."""
    return kiosk.onboarding_stage is OnboardingStage.LIVE and can_take_payment(
        db, kiosk
    )


def _as_response(db: Session, kiosk: Kiosk) -> StudentKioskResponse:
    prices = effective_prices(kiosk)
    remaining = sheets_remaining(db, kiosk)
    return StudentKioskResponse(
        id=kiosk.public_id,
        name=kiosk.name,
        accepts_wallet=kiosk.accepts_wallet,
        # Derived from the tray rather than carried as a flag, so it cannot
        # disagree with the number printed next to it.
        is_out_of_paper=remaining <= 0,
        sheets_remaining=remaining,
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
    return [_as_response(db, k) for k in kiosks if _printable(db, k)]


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
    return _as_response(db, kiosk)
