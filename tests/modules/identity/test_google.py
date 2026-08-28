import pytest

from app.core.errors import BadRequest
from app.modules.identity import repository as repo
from app.modules.identity.google import sign_in_with_google
from app.modules.identity.models import User
from app.modules.identity.roles import Role


@pytest.fixture
def verifier():
    """Stands in for Google's token verification. Never touches the network."""

    def _verify(token: str, client_id: str) -> dict:
        if token == "good":
            return {"email": "g@example.test", "name": "Gee", "email_verified": True}
        if token == "no-email":
            return {"name": "Gee"}
        if token == "unverified-email":
            return {"email": "g@example.test", "name": "Gee", "email_verified": False}
        raise ValueError("Invalid token")

    return _verify


def test_creates_an_account_on_first_sign_in(db_session, verifier):
    user = sign_in_with_google(db_session, "good", "client-id", verifier)
    db_session.flush()

    assert user.email == "g@example.test"
    assert user.full_name == "Gee"
    assert repo.roles_of(db_session, user.id) == {Role.STUDENT}


def test_a_google_account_is_verified_without_an_email_round_trip(db_session, verifier):
    """Google already proved the address; asking the user to confirm it again
    is friction for nothing."""
    user = sign_in_with_google(db_session, "good", "client-id", verifier)
    assert user.email_verified is True


def test_reuses_the_existing_account_on_later_sign_ins(db_session, verifier):
    first = sign_in_with_google(db_session, "good", "client-id", verifier)
    db_session.flush()
    second = sign_in_with_google(db_session, "good", "client-id", verifier)

    assert first.id == second.id


def test_links_to_an_account_registered_by_password(db_session, verifier):
    existing = User(email="g@example.test", hashed_password="x")
    db_session.add(existing)
    db_session.flush()

    user = sign_in_with_google(db_session, "good", "client-id", verifier)
    assert user.id == existing.id


def test_signing_in_with_google_verifies_a_previously_unverified_account(
    db_session, verifier
):
    existing = User(email="g@example.test", hashed_password="x", email_verified=False)
    db_session.add(existing)
    db_session.flush()

    sign_in_with_google(db_session, "good", "client-id", verifier)
    assert existing.email_verified is True


def test_an_invalid_token_is_rejected(db_session, verifier):
    with pytest.raises(BadRequest):
        sign_in_with_google(db_session, "bad", "client-id", verifier)


def test_the_reason_a_token_was_rejected_reaches_the_log(db_session, verifier, caplog):
    """The caller gets one generic sentence; the operator gets the real reason.

    Written after a valid Google token was refused in production and the only
    trace of it anywhere was a 400. google-auth says exactly what was wrong --
    "Token used too early" for a slow clock, "Wrong recipient" for a client id
    mismatch -- and this used to discard it, so those two, plus a genuinely
    forged token, were indistinguishable from the outside and from the logs.
    """
    with caplog.at_level("WARNING"):
        with pytest.raises(BadRequest) as refused:
            sign_in_with_google(db_session, "bad", "client-id", verifier)

    # The user is told nothing specific.
    assert "Invalid token" not in str(refused.value.detail)
    # The operator is told everything.
    assert "Invalid token" in caplog.text


def test_a_token_without_an_email_is_rejected(db_session, verifier):
    with pytest.raises(BadRequest):
        sign_in_with_google(db_session, "no-email", "client-id", verifier)


def test_a_google_account_with_an_unverified_address_is_rejected(db_session, verifier):
    """Otherwise someone could claim an account for a mailbox they do not own."""
    with pytest.raises(BadRequest):
        sign_in_with_google(db_session, "unverified-email", "client-id", verifier)


def test_sign_in_is_refused_when_google_is_not_configured(db_session, verifier):
    with pytest.raises(BadRequest):
        sign_in_with_google(db_session, "good", "", verifier)


def test_a_created_account_gets_an_unusable_password(db_session, verifier):
    user = sign_in_with_google(db_session, "good", "client-id", verifier)
    assert user.hashed_password
    assert user.hashed_password.startswith("$2")


def test_no_account_is_created_when_verification_fails(db_session, verifier):
    before = db_session.query(User).count()
    with pytest.raises(BadRequest):
        sign_in_with_google(db_session, "bad", "client-id", verifier)
    assert db_session.query(User).count() == before
