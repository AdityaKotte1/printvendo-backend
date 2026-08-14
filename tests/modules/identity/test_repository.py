import pytest

from app.modules.identity import repository as repo
from app.modules.identity.models import User, UserRole
from app.modules.identity.roles import Role


@pytest.fixture
def user(db_session) -> User:
    u = User(email="Person@Example.test", hashed_password="x", full_name="Person")
    db_session.add(u)
    db_session.flush()
    return u


def test_get_by_email_is_case_insensitive(db_session, user):
    assert repo.get_by_email(db_session, "person@example.test") is not None
    assert repo.get_by_email(db_session, "PERSON@EXAMPLE.TEST") is not None


def test_get_by_email_ignores_surrounding_whitespace(db_session, user):
    assert repo.get_by_email(db_session, "  person@example.test  ") is not None


def test_get_by_email_returns_none_when_absent(db_session):
    assert repo.get_by_email(db_session, "nobody@example.test") is None


def test_get_by_public_id(db_session, user):
    assert repo.get_by_public_id(db_session, user.public_id).id == user.id


def test_get_by_public_id_rejects_a_malformed_id(db_session):
    assert repo.get_by_public_id(db_session, "not-an-id") is None


def test_get_by_public_id_rejects_an_id_of_the_wrong_kind(db_session, user):
    """A kiosk id must never resolve to a user, even by accident."""
    wrong_kind = user.public_id.replace("usr_", "ksk_")
    assert repo.get_by_public_id(db_session, wrong_kind) is None


def test_roles_of_returns_an_empty_set_for_a_new_user(db_session, user):
    assert repo.roles_of(db_session, user.id) == set()


def test_grant_role_is_idempotent(db_session, user):
    repo.grant_role(db_session, user.id, Role.STUDENT)
    repo.grant_role(db_session, user.id, Role.STUDENT)
    db_session.flush()

    assert repo.roles_of(db_session, user.id) == {Role.STUDENT}
    assert db_session.query(UserRole).filter_by(user_id=user.id).count() == 1


def test_grant_role_adds_to_existing_roles(db_session, user):
    repo.grant_role(db_session, user.id, Role.STUDENT)
    repo.grant_role(db_session, user.id, Role.OWNER)
    db_session.flush()
    assert repo.roles_of(db_session, user.id) == {Role.STUDENT, Role.OWNER}


def test_revoke_role_removes_only_that_role(db_session, user):
    repo.grant_role(db_session, user.id, Role.STUDENT)
    repo.grant_role(db_session, user.id, Role.OWNER)
    db_session.flush()

    repo.revoke_role(db_session, user.id, Role.OWNER)
    db_session.flush()
    assert repo.roles_of(db_session, user.id) == {Role.STUDENT}


def test_inactive_users_are_not_returned_by_email(db_session, user):
    user.is_active = False
    db_session.flush()
    assert repo.get_by_email(db_session, user.email) is None


def test_inactive_users_are_not_returned_by_public_id(db_session, user):
    user.is_active = False
    db_session.flush()
    assert repo.get_by_public_id(db_session, user.public_id) is None


def test_email_exists_sees_a_deactivated_account(db_session, user):
    """A deactivated account still owns its address.

    Registration must refuse to reuse it, or the insert dies on the unique
    constraint with a much worse message.
    """
    user.is_active = False
    db_session.flush()

    assert repo.get_by_email(db_session, user.email) is None
    assert repo.email_exists(db_session, user.email) is True


def test_email_exists_is_case_insensitive(db_session, user):
    assert repo.email_exists(db_session, "PERSON@EXAMPLE.TEST") is True


def test_email_exists_is_false_for_an_unknown_address(db_session):
    assert repo.email_exists(db_session, "nobody@example.test") is False
