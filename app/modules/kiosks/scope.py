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


def system_scope() -> Scope:
    """Every kiosk, for work that is not being done on anybody's behalf.

    A scheduled sweep -- the offline watcher, the paper watcher -- has no actor
    to resolve a scope from, and must still see the whole estate. This is the
    one place that says so. The alternative is each caller writing
    `Scope(is_unrestricted=True, kiosk_ids=frozenset())` inline, which is the
    old backend's admin bypass again: the same decision made in several places,
    spelled slightly differently in each, and greppable from none of them.

    Not reachable from a request. Nothing in `app/api` calls it; a route that
    wanted this would be asking for an admin bypass and should say so out loud.
    """
    return Scope(is_unrestricted=True, kiosk_ids=frozenset())
