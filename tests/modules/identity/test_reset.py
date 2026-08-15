from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import BadRequest, Unauthorized
from app.core.security import verify_password
from app.modules.identity import sessions
from app.modules.identity.guests import create_guest
from app.modules.identity.models import OneTimeToken, User
from app.modules.identity.passwords import register
from app.modules.identity.reset import (
    TOKEN_LIFETIME,
    change_password,
    complete_reset,
    start_reset,
)
from app.modules.identity.tokens import _hash
from app.modules.identity.verification import start_verification

OLD = "correct horse battery"
NEW = "a different long password"
SECRET = "s" * 32


@pytest.fixture
def user(db_session) -> User:
    u = register(db_session, "person@example.com", OLD, None)
    db_session.flush()
    return u


def _row(db_session, token: str) -> OneTimeToken:
    return db_session.query(OneTimeToken).filter_by(token_hash=_hash(token)).one()


# ── requesting a reset ──────────────────────────────────────────────────────


def test_a_known_address_yields_a_user_and_token(db_session, user):
    result = start_reset(db_session, "person@example.com")
    assert result is not None
    assert result[0].id == user.id


def test_an_unknown_address_yields_nothing(db_session):
    assert start_reset(db_session, "nobody@example.com") is None


def test_lookup_is_case_insensitive(db_session, user):
    assert start_reset(db_session, "PERSON@EXAMPLE.COM") is not None


def test_a_guest_cannot_reset(db_session):
    """Their address is synthetic and unreachable, so a link goes nowhere while
    still confirming the account exists."""
    guest = create_guest(db_session)
    db_session.flush()
    assert start_reset(db_session, guest.email) is None


def test_a_deactivated_account_cannot_reset(db_session, user):
    user.is_active = False
    db_session.flush()
    assert start_reset(db_session, "person@example.com") is None


def test_the_token_is_stored_only_as_a_hash(db_session, user):
    _, token = start_reset(db_session, "person@example.com")
    db_session.flush()

    assert db_session.query(OneTimeToken).filter_by(token_hash=token).count() == 0
    assert _row(db_session, token) is not None


def test_a_reset_link_lives_an_hour_not_a_day(db_session, user):
    """A reset link is a live credential -- anyone holding it can take the
    account. Verification is a convenience and gets longer."""
    assert TOKEN_LIFETIME == timedelta(hours=1)


def test_requesting_again_invalidates_the_previous_link(db_session, user):
    _, first = start_reset(db_session, "person@example.com")
    db_session.flush()
    _, second = start_reset(db_session, "person@example.com")
    db_session.flush()

    with pytest.raises(BadRequest):
        complete_reset(db_session, first, NEW)
    assert complete_reset(db_session, second, NEW) is not None


# ── completing a reset ──────────────────────────────────────────────────────


def test_completing_sets_the_new_password(db_session, user):
    _, token = start_reset(db_session, "person@example.com")
    db_session.flush()

    complete_reset(db_session, token, NEW)
    db_session.flush()

    assert verify_password(NEW, user.hashed_password)
    assert not verify_password(OLD, user.hashed_password)


def test_a_reset_token_cannot_be_used_twice(db_session, user):
    _, token = start_reset(db_session, "person@example.com")
    db_session.flush()
    complete_reset(db_session, token, NEW)
    db_session.flush()

    with pytest.raises(BadRequest):
        complete_reset(db_session, token, "yet another password")


def test_an_expired_reset_token_is_refused(db_session, user):
    _, token = start_reset(db_session, "person@example.com")
    db_session.flush()

    _row(db_session, token).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    with pytest.raises(BadRequest):
        complete_reset(db_session, token, NEW)


def test_an_unknown_reset_token_is_refused(db_session):
    with pytest.raises(BadRequest):
        complete_reset(db_session, "never-issued", NEW)


def test_a_verification_token_cannot_reset_a_password(db_session, user):
    """Purposes must not be interchangeable: an email-confirmation link left in
    a mailbox would otherwise be an account takeover."""
    token = start_verification(db_session, user)
    db_session.flush()

    with pytest.raises(BadRequest):
        complete_reset(db_session, token, NEW)


def test_a_reset_token_cannot_verify_an_email(db_session, user):
    from app.modules.identity.verification import verify_email

    _, token = start_reset(db_session, "person@example.com")
    db_session.flush()

    with pytest.raises(BadRequest):
        verify_email(db_session, token)


def test_completing_a_reset_kills_every_session(db_session, user):
    """The likeliest reason for a reset is that someone else has the old
    password. Their refresh tokens must die with it."""
    _, phone = sessions.issue_tokens(db_session, user, SECRET)
    _, laptop = sessions.issue_tokens(db_session, user, SECRET)
    db_session.flush()

    _, token = start_reset(db_session, "person@example.com")
    db_session.flush()
    complete_reset(db_session, token, NEW)
    db_session.flush()

    for stale in (phone, laptop):
        with pytest.raises(Unauthorized):
            sessions.rotate_refresh(db_session, stale, SECRET)


# ── changing a password you know ────────────────────────────────────────────


def test_change_requires_the_current_password(db_session, user):
    """Without this, a stolen fifteen-minute access token becomes permanent
    account takeover."""
    with pytest.raises(Unauthorized):
        change_password(db_session, user, current="wrong", new=NEW)


def test_change_sets_the_new_password(db_session, user):
    change_password(db_session, user, current=OLD, new=NEW)
    db_session.flush()
    assert verify_password(NEW, user.hashed_password)


def test_change_refuses_the_same_password(db_session, user):
    with pytest.raises(BadRequest):
        change_password(db_session, user, current=OLD, new=OLD)


def test_a_failed_change_leaves_the_password_alone(db_session, user):
    before = user.hashed_password
    with pytest.raises(Unauthorized):
        change_password(db_session, user, current="wrong", new=NEW)
    assert user.hashed_password == before


def test_change_kills_every_session(db_session, user):
    """A password change is how someone evicts a session they no longer trust."""
    _, phone = sessions.issue_tokens(db_session, user, SECRET)
    db_session.flush()

    change_password(db_session, user, current=OLD, new=NEW)
    db_session.flush()

    with pytest.raises(Unauthorized):
        sessions.rotate_refresh(db_session, phone, SECRET)


def test_a_legacy_hash_user_can_change_their_password(db_session):
    """Migrated accounts must not be stuck on the old scheme."""
    from passlib.hash import pbkdf2_sha256

    legacy = User(
        email="old@example.com", hashed_password=pbkdf2_sha256.hash("legacy pass")
    )
    db_session.add(legacy)
    db_session.flush()

    change_password(db_session, legacy, current="legacy pass", new=NEW)
    db_session.flush()

    assert legacy.hashed_password.startswith("$2")
    assert verify_password(NEW, legacy.hashed_password)
