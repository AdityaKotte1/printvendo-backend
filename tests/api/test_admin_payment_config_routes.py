"""An admin reviewing where an owner's money is allowed to go.

This closes a loop that was open: an owner could submit a change request and
nothing in the system could answer it. The properties under test are the ones
that make the review meaningful rather than ceremonial --

* the proof is served as bytes through an authenticated route, never as a URL
  a browser might quietly fail to load;
* one approval authorises exactly one change;
* an owner cannot approve their own request.
"""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import (
    get_db,
    get_document_store,
    get_notifier,
    get_secret,
    get_secret_box,
)
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.ops import entries_for
from app.modules.printing import DocumentStore

SECRET = "s" * 32
BOX_KEY = Fernet.generate_key().decode()
KEY_SECRET = "rzp_live_secret_value"
PROOF_BYTES = b"\x89PNG\r\n\x1a\n-a-bank-statement"

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=BOX_KEY,
    CORS_ORIGINS="https://admin.printvendo.com",
    PUBLIC_BASE_URL="https://api.printvendo.com",
)


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    return DocumentStore(tmp_path)


@pytest.fixture
def client(db_session, store) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    app.dependency_overrides[get_secret_box] = lambda: SecretBox(BOX_KEY)
    app.dependency_overrides[get_document_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


def _user(db_session, email: str, *roles: Role) -> User:
    user = User(email=email, hashed_password="x")
    db_session.add(user)
    db_session.flush()
    for role in roles:
        identity_repo.grant_role(db_session, user.id, role)
    db_session.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    token = create_token(user.public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def owner(db_session) -> User:
    return _user(db_session, "shopkeeper@example.com", Role.OWNER)


@pytest.fixture
def owner_auth(owner) -> dict[str, str]:
    return _auth(owner)


@pytest.fixture
def admin(db_session) -> User:
    return _user(db_session, "ops@printvendo.com", Role.ADMIN)


@pytest.fixture
def admin_auth(admin) -> dict[str, str]:
    return _auth(admin)


QUEUE = "/v1/admin/payment-config/change-requests"


def _set_keys(client, auth, key_id="rzp_live_abc"):
    return client.put(
        "/v1/owner/payment-config/keys",
        headers=auth,
        json={"key_id": key_id, "key_secret": KEY_SECRET},
    )


def _request_change(client, auth, *, with_proof=True):
    files = (
        {"proof": ("statement.png", PROOF_BYTES, "image/png")} if with_proof else None
    )
    return client.post(
        "/v1/owner/payment-config/change-request",
        headers=auth,
        data={"reason": "moved banks"},
        files=files,
    )


def _queued_id(client, admin_auth) -> str:
    return client.get(QUEUE, headers=admin_auth).json()[0]["id"]


@pytest.fixture
def pending(client, owner_auth):
    """An owner with keys set who has asked to replace them."""
    _set_keys(client, owner_auth)
    _request_change(client, owner_auth)


# -- the queue --------------------------------------------------------------


def test_the_queue_names_the_owner_who_asked(client, admin_auth, owner, pending):
    listed = client.get(QUEUE, headers=admin_auth).json()

    assert len(listed) == 1
    assert listed[0]["owner_id"] == owner.public_id
    assert listed[0]["owner_email"] == owner.email
    assert listed[0]["reason"] == "moved banks"
    assert listed[0]["has_proof"] is True
    assert listed[0]["id"].startswith("pcr_")


def test_the_queue_never_carries_a_storage_path(client, admin_auth, pending):
    """A key in the body is an invitation to build a URL out of it, which is the
    mistake that got proofs approved unseen."""
    body = client.get(QUEUE, headers=admin_auth).text

    assert "proofs/" not in body
    assert "proof_path" not in body


def test_a_reviewed_request_leaves_the_queue(client, admin_auth, pending):
    request_id = _queued_id(client, admin_auth)

    client.post(
        f"{QUEUE}/{request_id}/review", headers=admin_auth, json={"approve": True}
    )

    assert client.get(QUEUE, headers=admin_auth).json() == []


# -- the proof --------------------------------------------------------------


def test_the_proof_is_served_as_bytes_to_an_admin(client, admin_auth, pending):
    request_id = _queued_id(client, admin_auth)

    response = client.get(f"{QUEUE}/{request_id}/proof", headers=admin_auth)

    assert response.status_code == 200
    assert response.content == PROOF_BYTES


def test_the_proof_is_refused_without_an_admin_token(
    client, owner_auth, admin_auth, pending
):
    request_id = _queued_id(client, admin_auth)

    anonymous = client.get(f"{QUEUE}/{request_id}/proof")
    as_owner = client.get(f"{QUEUE}/{request_id}/proof", headers=owner_auth)

    assert anonymous.status_code == 401
    assert as_owner.status_code == 403


def test_a_request_with_no_proof_is_a_clear_404(client, admin_auth, owner_auth):
    """Not an empty 200. "There is no proof" and "here is the proof, it is
    empty" must not look alike to somebody about to approve a change of bank
    details."""
    _set_keys(client, owner_auth)
    _request_change(client, owner_auth, with_proof=False)
    request_id = _queued_id(client, admin_auth)

    response = client.get(f"{QUEUE}/{request_id}/proof", headers=admin_auth)

    assert response.status_code == 404
    assert response.json()["detail"]


# -- the decision -----------------------------------------------------------


def test_approval_lets_the_owner_change_their_keys_exactly_once(
    client, admin_auth, owner_auth, pending
):
    request_id = _queued_id(client, admin_auth)

    reviewed = client.post(
        f"{QUEUE}/{request_id}/review",
        headers=admin_auth,
        json={"approve": True, "note": "statement checked"},
    )

    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert _set_keys(client, owner_auth, key_id="rzp_live_new").status_code == 200
    # One approval, one change. The second attempt has nothing left to consume.
    assert _set_keys(client, owner_auth, key_id="rzp_live_third").status_code == 409


def test_rejection_leaves_the_keys_where_they_are(
    client, admin_auth, owner_auth, pending
):
    request_id = _queued_id(client, admin_auth)

    client.post(
        f"{QUEUE}/{request_id}/review",
        headers=admin_auth,
        json={"approve": False, "note": "the account name does not match"},
    )

    assert _set_keys(client, owner_auth, key_id="rzp_live_attacker").status_code == 409


def test_the_same_request_cannot_be_reviewed_twice(client, admin_auth, pending):
    request_id = _queued_id(client, admin_auth)
    path = f"{QUEUE}/{request_id}/review"

    client.post(path, headers=admin_auth, json={"approve": True})
    second = client.post(path, headers=admin_auth, json={"approve": False})

    assert second.status_code == 409


def test_an_unknown_request_is_not_found(client, admin_auth):
    response = client.post(
        f"{QUEUE}/pcr_0000000000000000/review",
        headers=admin_auth,
        json={"approve": True},
    )

    assert response.status_code == 404


# -- who may do this --------------------------------------------------------


def test_an_owner_cannot_approve_their_own_request(client, owner_auth, pending):
    """The whole control. If an owner could reach this, the approval step would
    be a formality an account takeover walks straight through."""
    assert client.get(QUEUE, headers=owner_auth).status_code == 403


def test_the_queue_needs_a_token(client):
    assert client.get(QUEUE).status_code == 401


# -- the trail --------------------------------------------------------------


def test_the_decision_is_audited_against_the_owner(
    client, admin_auth, admin, owner, pending, db_session
):
    """Recorded against the owner, not the request, so "everything that has
    happened to this account's payment configuration" is one query -- the
    owner's own `change.requested` entry is already filed there."""
    request_id = _queued_id(client, admin_auth)

    client.post(
        f"{QUEUE}/{request_id}/review",
        headers=admin_auth,
        json={"approve": True, "note": "statement checked"},
    )

    trail = entries_for(db_session, entity_type="user", entity_id=owner.public_id)
    actions = [entry.action for entry in trail]

    assert "payment_config.change.reviewed" in actions
    reviewed = next(e for e in trail if e.action == "payment_config.change.reviewed")
    assert reviewed.actor_user_id == admin.id
    assert reviewed.after["status"] == "approved"
