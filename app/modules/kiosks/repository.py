"""Every read and write against the kiosk tables, all scoped.

`Scope` is the first argument of every function here, and there is no overload
that omits it. That is deliberate: it makes "I forgot to filter by owner"
impossible to write rather than merely discouraged.

Anything outside the caller's scope raises NotFound, never Forbidden. A 403
confirms that the kiosk exists, which tells an owner something about a
competitor's estate that they have no business learning.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.ids import IdPrefix, InvalidId, parse_id
from app.modules.kiosks.models import Kiosk
from app.modules.kiosks.scope import Scope

NO_SUCH_KIOSK = "That kiosk does not exist."


def list_kiosks(
    db: Session, scope: Scope, *, include_inactive: bool = False
) -> list[Kiosk]:
    stmt = select(Kiosk)
    if not scope.is_unrestricted:
        stmt = stmt.where(Kiosk.id.in_(scope.kiosk_ids))
    if not include_inactive:
        stmt = stmt.where(Kiosk.is_active.is_(True))
    return list(db.execute(stmt.order_by(Kiosk.name)).scalars())


def get_kiosk(db: Session, scope: Scope, public_id: str) -> Kiosk:
    """The kiosk, or NotFound -- whether it is missing or merely out of scope."""
    try:
        parse_id(public_id, IdPrefix.KIOSK)
    except InvalidId:
        raise NotFound(NO_SUCH_KIOSK) from None

    stmt = select(Kiosk).where(Kiosk.public_id == public_id)
    kiosk = db.execute(stmt).scalar_one_or_none()

    if kiosk is None or not scope.allows(kiosk.id):
        raise NotFound(NO_SUCH_KIOSK)
    return kiosk
