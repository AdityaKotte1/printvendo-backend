import dataclasses

import pytest

from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks.enums import AssignmentRole
from app.modules.kiosks.models import Kiosk, KioskAssignment
from app.modules.kiosks.scope import Scope, kiosk_scope


def _user(db_session, email: str, *roles: Role) -> User:
    user = User(email=email, hashed_password="x")
    db_session.add(user)
    db_session.flush()
    for role in roles:
        identity_repo.grant_role(db_session, user.id, role)
    db_session.flush()
    return user


def _kiosk(db_session, name: str) -> Kiosk:
    kiosk = Kiosk(name=name)
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


def _assign(db_session, kiosk, user, role: AssignmentRole) -> None:
    db_session.add(KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=role))
    db_session.flush()


def test_an_admin_scope_covers_everything(db_session):
    admin = _user(db_session, "admin@example.com", Role.ADMIN)
    assert kiosk_scope(db_session, admin).is_unrestricted is True


def test_an_owner_scope_lists_only_their_kiosks(db_session):
    owner = _user(db_session, "owner@example.com", Role.OWNER)
    mine = _kiosk(db_session, "Mine")
    _kiosk(db_session, "Theirs")
    _assign(db_session, mine, owner, AssignmentRole.OWNER)

    scope = kiosk_scope(db_session, owner)
    assert scope.is_unrestricted is False
    assert scope.kiosk_ids == {mine.id}


def test_a_refiller_scope_lists_only_their_kiosks(db_session):
    refiller = _user(db_session, "refiller@example.com", Role.REFILLER)
    mine = _kiosk(db_session, "Mine")
    _kiosk(db_session, "Theirs")
    _assign(db_session, mine, refiller, AssignmentRole.REFILLER)

    assert kiosk_scope(db_session, refiller).kiosk_ids == {mine.id}


def test_a_student_scope_is_empty(db_session):
    """A student has no administrative view of any kiosk. Browsing the public
    list is a different, unauthenticated read."""
    student = _user(db_session, "student@example.com", Role.STUDENT)
    _kiosk(db_session, "Somewhere")
    assert kiosk_scope(db_session, student).kiosk_ids == set()


def test_an_owner_of_two_kiosks_sees_both(db_session):
    owner = _user(db_session, "owner@example.com", Role.OWNER)
    a, b = _kiosk(db_session, "A"), _kiosk(db_session, "B")
    _assign(db_session, a, owner, AssignmentRole.OWNER)
    _assign(db_session, b, owner, AssignmentRole.OWNER)

    assert kiosk_scope(db_session, owner).kiosk_ids == {a.id, b.id}


def test_two_owners_do_not_see_each_others_kiosks(db_session):
    """The whole point of this module."""
    alice = _user(db_session, "alice@example.com", Role.OWNER)
    bob = _user(db_session, "bob@example.com", Role.OWNER)
    a, b = _kiosk(db_session, "Alice Shop"), _kiosk(db_session, "Bob Shop")
    _assign(db_session, a, alice, AssignmentRole.OWNER)
    _assign(db_session, b, bob, AssignmentRole.OWNER)

    assert kiosk_scope(db_session, alice).kiosk_ids == {a.id}
    assert kiosk_scope(db_session, bob).kiosk_ids == {b.id}


def test_admin_scope_wins_even_when_also_an_owner(db_session):
    admin = _user(db_session, "both@example.com", Role.ADMIN, Role.OWNER)
    _kiosk(db_session, "Somewhere")
    assert kiosk_scope(db_session, admin).is_unrestricted is True


def test_scope_allows_checks_a_single_id(db_session):
    owner = _user(db_session, "owner@example.com", Role.OWNER)
    mine, theirs = _kiosk(db_session, "Mine"), _kiosk(db_session, "Theirs")
    _assign(db_session, mine, owner, AssignmentRole.OWNER)

    scope = kiosk_scope(db_session, owner)
    assert scope.allows(mine.id) is True
    assert scope.allows(theirs.id) is False


def test_an_unrestricted_scope_allows_any_id(db_session):
    admin = _user(db_session, "admin@example.com", Role.ADMIN)
    assert kiosk_scope(db_session, admin).allows(9999) is True


def test_an_empty_scope_is_not_unrestricted(db_session):
    """A bug that treats "no kiosks" as "all kiosks" is the worst possible
    failure here, so the two must not be confusable."""
    student = _user(db_session, "student@example.com", Role.STUDENT)
    scope = kiosk_scope(db_session, student)
    assert scope.is_unrestricted is False
    assert scope.allows(1) is False


def test_scope_cannot_be_constructed_unrestricted_by_accident():
    """`Scope()` meaning "everything" must not be expressible."""
    with pytest.raises(TypeError):
        Scope()


def test_scope_is_immutable(db_session):
    """Nothing downstream may widen a scope it was handed."""
    owner = _user(db_session, "owner@example.com", Role.OWNER)
    scope = kiosk_scope(db_session, owner)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.is_unrestricted = True


def test_a_refiller_at_one_kiosk_of_an_owner_with_many(db_session):
    """Assignment is per kiosk, not per owner: covering one shop must not
    expose the rest of that owner's estate."""
    owner = _user(db_session, "owner@example.com", Role.OWNER)
    refiller = _user(db_session, "refiller@example.com", Role.REFILLER)
    a, b = _kiosk(db_session, "Shop A"), _kiosk(db_session, "Shop B")
    _assign(db_session, a, owner, AssignmentRole.OWNER)
    _assign(db_session, b, owner, AssignmentRole.OWNER)
    _assign(db_session, a, refiller, AssignmentRole.REFILLER)

    assert kiosk_scope(db_session, refiller).kiosk_ids == {a.id}
