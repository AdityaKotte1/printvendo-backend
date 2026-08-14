import pytest
from passlib.hash import pbkdf2_sha256

from app.core.errors import Conflict, Unauthorized
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.passwords import authenticate, register
from app.modules.identity.roles import Role


def test_register_creates_a_student(db_session):
    user = register(db_session, "new@example.test", "correct horse battery", "New")
    db_session.flush()

    assert user.email == "new@example.test"
    assert repo.roles_of(db_session, user.id) == {Role.STUDENT}
    assert user.is_guest is False


def test_register_does_not_store_the_password(db_session):
    user = register(db_session, "new@example.test", "correct horse battery", None)
    assert "correct horse battery" not in user.hashed_password


def test_register_rejects_a_duplicate_email(db_session):
    register(db_session, "dup@example.test", "password one", None)
    db_session.flush()
    with pytest.raises(Conflict):
        register(db_session, "dup@example.test", "password two", None)


def test_register_rejects_a_duplicate_email_differing_only_by_case(db_session):
    register(db_session, "Dup@Example.test", "password one", None)
    db_session.flush()
    with pytest.raises(Conflict):
        register(db_session, "dup@example.TEST", "password two", None)


def test_authenticate_accepts_the_right_password(db_session):
    register(db_session, "a@example.test", "hunter2hunter2", None)
    db_session.flush()
    assert authenticate(db_session, "a@example.test", "hunter2hunter2") is not None


def test_authenticate_rejects_the_wrong_password(db_session):
    register(db_session, "a@example.test", "hunter2hunter2", None)
    db_session.flush()
    with pytest.raises(Unauthorized):
        authenticate(db_session, "a@example.test", "wrong")


def test_authenticate_rejects_an_unknown_email(db_session):
    with pytest.raises(Unauthorized):
        authenticate(db_session, "nobody@example.test", "whatever")


def test_the_two_failures_are_indistinguishable(db_session):
    """Different messages for "no such user" and "wrong password" let an
    attacker enumerate which emails have accounts."""
    register(db_session, "a@example.test", "hunter2hunter2", None)
    db_session.flush()

    with pytest.raises(Unauthorized) as wrong_password:
        authenticate(db_session, "a@example.test", "wrong")
    with pytest.raises(Unauthorized) as no_such_user:
        authenticate(db_session, "nobody@example.test", "wrong")

    assert str(wrong_password.value) == str(no_such_user.value)


def test_authenticate_refuses_a_deactivated_account(db_session):
    user = register(db_session, "off@example.test", "hunter2hunter2", None)
    db_session.flush()
    user.is_active = False
    db_session.flush()

    with pytest.raises(Unauthorized):
        authenticate(db_session, "off@example.test", "hunter2hunter2")


def test_a_legacy_pbkdf2_hash_is_upgraded_on_successful_login(db_session):
    """Migrated users arrive with pbkdf2 hashes; logging in moves them to bcrypt."""
    user = User(email="old@example.test", hashed_password=pbkdf2_sha256.hash("legacy pass"))
    db_session.add(user)
    db_session.flush()
    assert user.hashed_password.startswith("$pbkdf2-sha256$")

    authenticate(db_session, "old@example.test", "legacy pass")
    db_session.flush()

    assert user.hashed_password.startswith("$2")
    assert authenticate(db_session, "old@example.test", "legacy pass") is not None


def test_a_failed_login_does_not_rehash(db_session):
    user = User(email="old@example.test", hashed_password=pbkdf2_sha256.hash("legacy pass"))
    db_session.add(user)
    db_session.flush()
    before = user.hashed_password

    with pytest.raises(Unauthorized):
        authenticate(db_session, "old@example.test", "wrong")

    assert user.hashed_password == before
