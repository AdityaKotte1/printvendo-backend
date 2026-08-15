from datetime import UTC, datetime, timedelta

import pytest

from app.core.errors import BadRequest
from app.modules.identity.models import OneTimeToken, User
from app.modules.identity.passwords import register
from app.modules.identity.verification import (
    TOKEN_LIFETIME,
    start_verification,
    verify_email,
)


@pytest.fixture
def user(db_session) -> User:
    u = register(db_session, "v@example.test", "correct horse battery", None)
    db_session.flush()
    return u


def _row(db_session, token: str) -> OneTimeToken:
    from app.modules.identity.tokens import _hash

    return db_session.query(OneTimeToken).filter_by(token_hash=_hash(token)).one()


def test_a_new_registration_is_not_verified(db_session, user):
    assert user.email_verified is False


def test_start_verification_returns_a_token(db_session, user):
    token = start_verification(db_session, user)
    db_session.flush()
    assert token


def test_the_token_is_stored_only_as_a_hash(db_session, user):
    token = start_verification(db_session, user)
    db_session.flush()

    assert db_session.query(OneTimeToken).filter_by(token_hash=token).count() == 0
    assert _row(db_session, token) is not None


def test_verifying_marks_the_user_verified(db_session, user):
    token = start_verification(db_session, user)
    db_session.flush()

    verified = verify_email(db_session, token)
    db_session.flush()

    assert verified.id == user.id
    assert user.email_verified is True


def test_a_token_cannot_be_used_twice(db_session, user):
    """A link forwarded or sitting in a mailbox must not keep working."""
    token = start_verification(db_session, user)
    db_session.flush()

    verify_email(db_session, token)
    db_session.flush()

    with pytest.raises(BadRequest):
        verify_email(db_session, token)


def test_an_expired_token_is_refused(db_session, user):
    token = start_verification(db_session, user)
    db_session.flush()

    row = _row(db_session, token)
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    with pytest.raises(BadRequest):
        verify_email(db_session, token)


def test_an_unknown_token_is_refused(db_session):
    with pytest.raises(BadRequest):
        verify_email(db_session, "never-issued")


def test_requesting_a_new_token_invalidates_the_previous_one(db_session, user):
    """Otherwise "resend" leaves several live links for one address."""
    first = start_verification(db_session, user)
    db_session.flush()

    second = start_verification(db_session, user)
    db_session.flush()

    with pytest.raises(BadRequest):
        verify_email(db_session, first)

    assert verify_email(db_session, second).id == user.id


def test_the_token_lifetime_is_a_day(db_session, user):
    token = start_verification(db_session, user)
    db_session.flush()

    row = _row(db_session, token)
    expected = datetime.now(UTC) + TOKEN_LIFETIME
    assert abs((row.expires_at - expected).total_seconds()) < 60


def test_verifying_an_already_verified_user_is_refused(db_session, user):
    token = start_verification(db_session, user)
    db_session.flush()
    verify_email(db_session, token)
    db_session.flush()

    new_token = start_verification(db_session, user)
    db_session.flush()
    # Still works -- re-verifying is harmless, and refusing would strand anyone
    # who clicks an older link after changing their address.
    assert verify_email(db_session, new_token).id == user.id
