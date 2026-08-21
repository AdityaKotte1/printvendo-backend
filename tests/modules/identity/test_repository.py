import pytest

from app.core.errors import Unauthorized
from app.modules.identity import repository as repo
from app.modules.identity.accounts import set_active
from app.modules.identity.models import User, UserRole
from app.modules.identity.roles import Role
from app.modules.identity.sessions import issue_tokens, rotate_refresh


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


def test_actors_by_id_resolves_a_batch(db_session):
    """An audit trail names its actors by internal id. Turning a page of those
    into people must be one query, not one per row -- the N+1 that made the old
    backend's admin listings slow enough that nobody opened them."""
    first = User(email="one@example.com", hashed_password="x")
    second = User(email="two@example.com", hashed_password="x")
    db_session.add_all([first, second])
    db_session.flush()

    found = repo.actors_by_id(db_session, {first.id, second.id, 99999})

    assert found[first.id].email == "one@example.com"
    assert found[second.id].public_id == second.public_id
    # An actor whose account has since been deleted is simply absent, not an
    # error: an audit entry outlives the person it names, and must still read.
    assert 99999 not in found


def test_actors_by_id_asks_nothing_when_there_is_nobody_to_ask_about(db_session):
    assert repo.actors_by_id(db_session, set()) == {}


def test_deactivating_kills_sessions_that_reactivating_does_not_bring_back(db_session):
    """The property `revoke_all` is actually load-bearing for.

    `rotate_refresh` already refuses an inactive user, so a refresh attempted
    *while* the account is off fails either way -- a test of that would pass
    without the revocation and prove nothing. What only holds with it is that a
    refresh token taken before the account was switched off is still dead after
    it is switched back on. Found by deliberately removing the revocation and
    watching every test still pass.
    """
    secret = "s" * 32
    user = User(email="leaver@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()

    _, refresh = issue_tokens(db_session, user, secret)
    db_session.flush()

    set_active(db_session, user, is_active=False)
    set_active(db_session, user, is_active=True)
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, refresh, secret)
