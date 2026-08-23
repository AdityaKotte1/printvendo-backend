"""Shops a student saved, so the picker can put them first.

Deliberately three functions and no more. Saving and unsaving are idempotent
because the star is a toggle on an unreliable network: a repeated tap, or a
retried request, must not produce an error somebody has to read.

`favourite_kiosk_ids` returns a **set of internal ids**, which the api layer
uses to mark up a list it has already fetched and scoped. It is not a read of
kiosks -- there is still no unscoped read of those -- and it deliberately cannot
answer the reverse question, which is who saved a given shop.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.kiosks.models import Kiosk, KioskFavourite


def favourite_kiosk(db: Session, *, user_id: int, kiosk: Kiosk) -> None:
    """Save a shop. Saving one already saved changes nothing."""
    existing = db.execute(
        select(KioskFavourite.id).where(
            KioskFavourite.user_id == user_id,
            KioskFavourite.kiosk_id == kiosk.id,
        )
    ).first()
    if existing is not None:
        return

    db.add(KioskFavourite(user_id=user_id, kiosk_id=kiosk.id))
    try:
        db.flush()
    except IntegrityError:
        # Two taps raced past the SELECT. The unique constraint decides, and
        # the outcome either way is that the shop is saved.
        db.rollback()


def unfavourite_kiosk(db: Session, *, user_id: int, kiosk: Kiosk) -> None:
    """Forget a shop. Forgetting one that was never saved is not an error."""
    db.query(KioskFavourite).filter(
        KioskFavourite.user_id == user_id,
        KioskFavourite.kiosk_id == kiosk.id,
    ).delete()
    db.flush()


def favourite_kiosk_ids(db: Session, *, user_id: int) -> set[int]:
    """Which shops this student saved.

    An empty set means none, which is emphatically not the same as all -- the
    caller filters a list with this, and the distinction is the difference
    between an empty section and every shop marked as a favourite.
    """
    stmt = select(KioskFavourite.kiosk_id).where(KioskFavourite.user_id == user_id)
    return set(db.execute(stmt).scalars())
