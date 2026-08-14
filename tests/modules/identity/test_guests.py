import re

from app.modules.identity import repository as repo
from app.modules.identity.guests import create_guest
from app.modules.identity.roles import Role

GUEST_EMAIL = re.compile(r"^guest_[0-9a-f]{32}@guest\.printvendo$")


def test_guest_is_flagged_as_a_guest(db_session):
    guest = create_guest(db_session)
    db_session.flush()
    assert guest.is_guest is True


def test_guest_holds_the_student_role(db_session):
    guest = create_guest(db_session)
    db_session.flush()
    assert repo.roles_of(db_session, guest.id) == {Role.STUDENT}


def test_guest_email_follows_the_synthetic_pattern(db_session):
    guest = create_guest(db_session)
    assert GUEST_EMAIL.match(guest.email), guest.email


def test_guests_get_distinct_emails(db_session):
    emails = {create_guest(db_session).email for _ in range(50)}
    assert len(emails) == 50


def test_guest_password_is_random_and_unusable(db_session):
    a = create_guest(db_session)
    b = create_guest(db_session)
    assert a.hashed_password != b.hashed_password
    assert a.hashed_password.startswith("$2")


def test_a_guest_gets_a_public_id_like_any_user(db_session):
    guest = create_guest(db_session)
    db_session.flush()
    assert guest.public_id.startswith("usr_")
