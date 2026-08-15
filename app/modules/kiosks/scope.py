"""Which kiosks an actor may act on.

One resolver, used by every kiosk-scoped read and write. The backend being
replaced put admin access in a separate router that filtered by the id in the
URL without checking ownership -- safe only because a dependency restricted it
to admins, and carrying a comment begging nobody to loosen it. Here admin is not
a separate path: it is this function returning an unrestricted scope.

Scope has no default constructor. `Scope()` meaning "everything" is the single
most dangerous typo available in this codebase, so it is not expressible.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.identity import User
from app.modules.identity import repository as identity_repo
from app.modules.identity.roles import Role
from app.modules.kiosks.models import KioskAssignment


@dataclass(frozen=True)
class Scope:
    """The set of kiosks an actor may touch.

    `is_unrestricted` is admin. Otherwise `kiosk_ids` is exhaustive -- an empty
    set means no access at all, which is emphatically not the same as full
    access.
    """

    is_unrestricted: bool
    kiosk_ids: frozenset[int]

    def allows(self, kiosk_id: int) -> bool:
        return self.is_unrestricted or kiosk_id in self.kiosk_ids


def kiosk_scope(db: Session, actor: User) -> Scope:
    roles = identity_repo.roles_of(db, actor.id)

    if Role.ADMIN in roles:
        return Scope(is_unrestricted=True, kiosk_ids=frozenset())

    stmt = select(KioskAssignment.kiosk_id).where(KioskAssignment.user_id == actor.id)
    assigned = frozenset(db.execute(stmt).scalars())
    return Scope(is_unrestricted=False, kiosk_ids=assigned)
