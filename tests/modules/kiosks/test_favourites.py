"""A student's saved shops.

Small, and worth being careful about anyway: it is the first table that records
something about a *student's* relationship to a kiosk, and the temptation is to
let it answer questions it should not. It says which shops this person saved. It
does not say who saved a shop -- that is a question about a student, asked from
the kiosk's side, and nobody has any business asking it.
"""

import pytest

from app.modules.identity.models import User
from app.modules.kiosks import (
    favourite_kiosk,
    favourite_kiosk_ids,
    unfavourite_kiosk,
)
from app.modules.kiosks.models import Kiosk


@pytest.fixture
def student(db_session) -> User:
    user = User(email="saver@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def other(db_session) -> User:
    user = User(email="somebody.else@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


def _kiosk(db_session, name: str) -> Kiosk:
    kiosk = Kiosk(name=name)
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


def test_a_saved_shop_comes_back(db_session, student):
    kiosk = _kiosk(db_session, "Corner Print")

    favourite_kiosk(db_session, user_id=student.id, kiosk=kiosk)

    assert favourite_kiosk_ids(db_session, user_id=student.id) == {kiosk.id}


def test_saving_twice_is_not_an_error(db_session, student):
    """Two taps on a star, or a retried request. The second changes nothing."""
    kiosk = _kiosk(db_session, "Corner Print")

    favourite_kiosk(db_session, user_id=student.id, kiosk=kiosk)
    favourite_kiosk(db_session, user_id=student.id, kiosk=kiosk)

    assert favourite_kiosk_ids(db_session, user_id=student.id) == {kiosk.id}


def test_unsaving_removes_it(db_session, student):
    kiosk = _kiosk(db_session, "Corner Print")
    favourite_kiosk(db_session, user_id=student.id, kiosk=kiosk)

    unfavourite_kiosk(db_session, user_id=student.id, kiosk=kiosk)

    assert favourite_kiosk_ids(db_session, user_id=student.id) == set()


def test_unsaving_something_that_was_never_saved_is_not_an_error(db_session, student):
    """The star is a toggle and the network is unreliable; a repeated tap must
    not produce an error the student has to read."""
    kiosk = _kiosk(db_session, "Corner Print")

    unfavourite_kiosk(db_session, user_id=student.id, kiosk=kiosk)

    assert favourite_kiosk_ids(db_session, user_id=student.id) == set()


def test_one_students_saved_shops_are_not_anothers(db_session, student, other):
    mine = _kiosk(db_session, "Mine")
    theirs = _kiosk(db_session, "Theirs")
    favourite_kiosk(db_session, user_id=student.id, kiosk=mine)
    favourite_kiosk(db_session, user_id=other.id, kiosk=theirs)

    assert favourite_kiosk_ids(db_session, user_id=student.id) == {mine.id}
    assert favourite_kiosk_ids(db_session, user_id=other.id) == {theirs.id}


def test_nothing_saved_is_an_empty_set_rather_than_everything(db_session, student):
    """The distinction that matters when a caller filters a list by this."""
    _kiosk(db_session, "Corner Print")

    assert favourite_kiosk_ids(db_session, user_id=student.id) == set()
