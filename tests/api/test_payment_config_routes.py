"""An owner setting up where their takings go.

The properties under test are the ones the old backend got wrong: no endpoint
returns a key secret, keys cannot be silently replaced, and the whole thing is
what stands between a SOLD kiosk and being able to print.
"""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret, get_secret_box
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.payments.configs import decrypt_secret, get_config

SECRET = "s" * 32
BOX_KEY = Fernet.generate_key().decode()
KEY_SECRET = "rzp_live_secret_value"

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=BOX_KEY,
    CORS_ORIGINS="https://owner.printvendo.com",
    PUBLIC_BASE_URL="https://api.printvendo.com",
)


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    app.dependency_overrides[get_secret_box] = lambda: SecretBox(BOX_KEY)
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
def auth(owner) -> dict[str, str]:
    return _auth(owner)


def set_keys(client, auth, *, key_id="rzp_live_abc", key_secret=KEY_SECRET):
    return client.put(
        "/v1/owner/payment-config/keys",
        headers=auth,
        json={"key_id": key_id, "key_secret": key_secret},
    )


# ── the secret never comes back ─────────────────────────────────────────────


def test_no_endpoint_ever_returns_the_key_secret(client, auth, owner):
    """The defect this whole module exists to prevent: the old backend stored
    the secret in plaintext while claiming otherwise, so a dump handed over
    every owner's live credentials."""
    set_keys(client, auth)

    for path in ("", "/webhook-endpoint"):
        body = client.get(f"/v1/owner/payment-config{path}", headers=auth).text
        assert KEY_SECRET not in body

    assert KEY_SECRET not in set_keys(client, auth).text


def test_the_key_id_comes_back_masked(client, auth):
    set_keys(client, auth, key_id="rzp_live_abcdefgh")

    body = client.get("/v1/owner/payment-config", headers=auth).json()

    assert body["is_configured"] is True
    assert body["key_id_masked"] != "rzp_live_abcdefgh"
    assert "abcdefgh" not in body["key_id_masked"]


def test_the_secret_is_encrypted_at_rest(client, auth, owner, db_session):
    """Not hashed and not plaintext -- it has to be recoverable to sign a
    charge, so the column holds ciphertext and only the SecretBox opens it."""
    set_keys(client, auth)

    config = get_config(db_session, owner.id)
    assert config.razorpay_key_secret_encrypted != KEY_SECRET
    assert KEY_SECRET not in config.razorpay_key_secret_encrypted
    assert decrypt_secret(config, SecretBox(BOX_KEY)) == KEY_SECRET


# ── set once, then only by approval ─────────────────────────────────────────


def test_keys_cannot_be_silently_replaced(client, auth):
    """The anti-fraud control. Owners are paid directly, so there is no
    settlement run where a redirected payment would surface -- an account
    takeover could point every kiosk's takings at a new account and the owner
    would notice when the money stopped."""
    assert set_keys(client, auth).status_code == 200

    second = set_keys(client, auth, key_id="rzp_live_attacker")

    # 409, not 400: the request is well formed, the account's state refuses it.
    assert second.status_code == 409
    assert client.get("/v1/owner/payment-config", headers=auth).json()["can_update"] is False


def test_a_change_request_is_accepted_and_leaves_the_keys_alone(client, auth):
    set_keys(client, auth)

    requested = client.post(
        "/v1/owner/payment-config/change-request",
        headers=auth,
        data={"reason": "moved banks"},
    )

    assert requested.status_code == 202
    # Requesting is not approval. Until an admin acts, the keys stay put.
    assert set_keys(client, auth, key_id="rzp_live_attacker").status_code == 409


# ── the webhook URL ─────────────────────────────────────────────────────────


def test_the_owner_is_handed_their_own_webhook_url(client, auth, owner):
    """Constructed for them because a typo here is silent: deliveries would
    arrive at a URL naming a different account, fail the signature check, and
    the owner's payments would never settle with nothing to see."""
    body = client.get("/v1/owner/payment-config/webhook-endpoint", headers=auth).json()

    assert body["url"] == (
        f"https://api.printvendo.com/v1/webhooks/razorpay/{owner.public_id}"
    )
    assert "refund.processed" in body["events"]


# ── who may do this ─────────────────────────────────────────────────────────


def test_a_student_cannot_configure_payment_keys(client, db_session):
    student = _user(db_session, "student@example.com")

    response = set_keys(client, _auth(student))

    assert response.status_code == 403


def test_signing_in_is_required(client):
    assert client.get("/v1/owner/payment-config").status_code == 401


def test_one_owner_cannot_see_anothers_configuration(client, auth, db_session):
    """Each route acts on the caller's own account, resolved from the token --
    there is no id in the path to tamper with."""
    set_keys(client, auth, key_id="rzp_live_first")

    other = _user(db_session, "other@example.com", Role.OWNER)
    body = client.get("/v1/owner/payment-config", headers=_auth(other)).json()

    assert body["is_configured"] is False
    assert body["key_id_masked"] is None


# ── it is written down ──────────────────────────────────────────────────────


def test_setting_keys_leaves_an_audit_entry_without_the_secret(
    client, auth, owner, db_session
):
    set_keys(client, auth)

    from app.modules.ops import entries_for

    entries = entries_for(db_session, entity_type="user", entity_id=owner.public_id)
    assert [e.action for e in entries] == ["payment_config.keys.set"]
    assert KEY_SECRET not in str(entries[0].after)
    assert entries[0].actor_user_id == owner.id
