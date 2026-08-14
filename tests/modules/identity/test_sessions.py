from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import Unauthorized
from app.core.security import TokenType, decode_token
from app.modules.identity.models import RefreshToken, User
from app.modules.identity.sessions import (
    GRACE_SECONDS,
    _hash,
    issue_tokens,
    revoke_all,
    revoke_refresh,
    rotate_refresh,
)

SECRET = "s" * 32


@pytest.fixture
def user(db_session) -> User:
    u = User(email="s@example.test", hashed_password="x")
    db_session.add(u)
    db_session.flush()
    return u


def _row(db_session, token: str) -> RefreshToken:
    return db_session.query(RefreshToken).filter_by(token_hash=_hash(token)).one()


def _age_out(db_session, token: str) -> None:
    """Push a token's revocation past the grace window."""
    row = _row(db_session, token)
    row.revoked_at = datetime.now(UTC) - timedelta(seconds=GRACE_SECONDS + 5)
    db_session.flush()


def test_issue_returns_an_access_token_carrying_the_public_id(db_session, user):
    access, _ = issue_tokens(db_session, user, SECRET)
    assert decode_token(access, TokenType.ACCESS, SECRET).subject == user.public_id


def test_the_access_token_never_carries_the_row_id(db_session, user):
    access, _ = issue_tokens(db_session, user, SECRET)
    assert decode_token(access, TokenType.ACCESS, SECRET).subject != str(user.id)


def test_issue_stores_only_a_hash_of_the_refresh_token(db_session, user):
    _, refresh = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    assert db_session.query(RefreshToken).filter_by(token_hash=refresh).count() == 0
    assert _row(db_session, refresh) is not None


def test_rotation_issues_a_new_refresh_token(db_session, user):
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    _, second = rotate_refresh(db_session, first, SECRET)
    assert second != first


def test_rotation_keeps_the_family(db_session, user):
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    _, second = rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    assert _row(db_session, second).family_id == _row(db_session, first).family_id


def test_a_concurrent_refresh_inside_the_grace_window_succeeds(db_session, user):
    """Two tabs refreshing at once must both work.

    Revoking instantly is what caused the old backend's "logs out frequently"
    bug: the losing tab was told its token was invalid and the client bounced
    the user to /login.
    """
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    access, again = rotate_refresh(db_session, first, SECRET)
    assert access
    assert again


def test_a_replay_after_the_grace_window_is_refused(db_session, user):
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    _age_out(db_session, first)

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)


def test_a_replay_after_the_grace_window_kills_the_whole_family(db_session, user):
    """A token replayed long after rotation means it was stolen. Every
    descendant of that login must die, not just the one presented."""
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    _, second = rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    _age_out(db_session, first)

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, second, SECRET)


def test_a_replay_does_not_kill_a_different_login(db_session, user):
    """Revoking the family must not sign the user out on their other devices."""
    _, phone = issue_tokens(db_session, user, SECRET)
    _, laptop = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    rotate_refresh(db_session, phone, SECRET)
    db_session.flush()
    _age_out(db_session, phone)

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, phone, SECRET)
    db_session.flush()

    access, _ = rotate_refresh(db_session, laptop, SECRET)
    assert access


def test_an_expired_token_is_refused_even_inside_the_grace_window(db_session, user):
    """Grace covers revocation, never expiry. Expiry is the real deadline."""
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    row = _row(db_session, first)
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)


def test_an_unknown_token_is_refused(db_session):
    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, "never-issued", SECRET)


def test_logout_revokes_immediately_with_no_grace(db_session, user):
    """Someone signing out on a shared machine must be signed out now, not in
    a minute."""
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    revoke_refresh(db_session, first)
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)


def test_revoke_all_kills_every_session(db_session, user):
    _, a = issue_tokens(db_session, user, SECRET)
    _, b = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    revoke_all(db_session, user.id)
    db_session.flush()

    for token in (a, b):
        with pytest.raises(Unauthorized):
            rotate_refresh(db_session, token, SECRET)


def test_separate_logins_are_separate_families(db_session, user):
    _, a = issue_tokens(db_session, user, SECRET)
    _, b = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    assert _row(db_session, a).family_id != _row(db_session, b).family_id


def test_a_deactivated_user_cannot_refresh(db_session, user):
    _, first = issue_tokens(db_session, user, SECRET)
    db_session.flush()

    user.is_active = False
    db_session.flush()

    with pytest.raises(Unauthorized):
        rotate_refresh(db_session, first, SECRET)


def test_every_failure_gives_the_same_message(db_session, user):
    """Distinguishing "expired" from "replayed" tells an attacker whether a
    stolen token was ever valid."""
    _, unknown = "x", "never-issued"

    _, expired = issue_tokens(db_session, user, SECRET)
    db_session.flush()
    row = _row(db_session, expired)
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    messages = set()
    for token in (unknown, expired):
        with pytest.raises(Unauthorized) as caught:
            rotate_refresh(db_session, token, SECRET)
        messages.add(str(caught.value))

    assert len(messages) == 1
