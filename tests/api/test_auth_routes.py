import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret
from app.core.config import Settings
from app.main import create_app

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="http://localhost:3000",
)

PASSWORD = "correct horse battery"


class RecordingNotifier:
    """Captures what would have been emailed, so tests can read the token."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_email_verification(self, *, email: str, token: str) -> None:
        self.sent.append((email, token))


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def client(db_session, notifier) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: notifier
    return TestClient(app, raise_server_exceptions=False)


def _register(client: TestClient, email: str = "a@example.com", **extra) -> dict:
    body = {"email": email, "password": PASSWORD, **extra}
    return client.post("/v1/app/auth/register", json=body)


def _login(client: TestClient, email: str = "a@example.com", password: str = PASSWORD):
    return client.post("/v1/app/auth/login", json={"email": email, "password": password})


def test_register_then_login(client):
    registered = _register(client, full_name="New")
    assert registered.status_code == 201, registered.text

    logged_in = _login(client)
    assert logged_in.status_code == 200
    assert logged_in.json()["access_token"]


def test_register_rejects_a_duplicate_with_409(client):
    _register(client, "dup@example.com")
    again = _register(client, "dup@example.com")
    assert again.status_code == 409
    assert "already exists" in again.json()["detail"]


def test_register_rejects_a_short_password(client):
    response = client.post(
        "/v1/app/auth/register", json={"email": "a@example.com", "password": "short"}
    )
    assert response.status_code == 422


def test_register_rejects_a_malformed_email(client):
    response = client.post(
        "/v1/app/auth/register", json={"email": "not-an-email", "password": PASSWORD}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "email",
    ["a@example.test", "a@somewhere.local", "a@localhost", "a@thing.invalid"],
)
def test_register_rejects_reserved_and_undeliverable_domains(client, email):
    """RFC 2606 special-use domains cannot receive mail.

    Accepting one means an account whose verification email can never arrive
    and whose owner can never be contacted about a payment. This is deliberate,
    not incidental -- it is why the tests here use example.com.
    """
    response = client.post(
        "/v1/app/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 422, email


def test_login_with_a_wrong_password_is_401(client):
    _register(client)
    assert _login(client, password="nope").status_code == 401


def test_login_for_an_unknown_email_is_401(client):
    assert _login(client, "nobody@example.com").status_code == 401


def test_the_refresh_token_is_never_in_the_response_body(client):
    registered = _register(client)
    assert "refresh_token" not in registered.text


def test_the_refresh_cookie_is_httponly_and_lax(client):
    _register(client)
    cookie = _login(client).headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie


def test_guest_login_returns_a_token_and_flags_the_account(client):
    response = client.post("/v1/app/auth/guest")
    assert response.status_code == 200
    assert response.json()["is_guest"] is True


def test_me_requires_a_token(client):
    assert client.get("/v1/app/auth/me").status_code == 401


def test_me_returns_the_signed_in_user(client):
    _register(client, full_name="Ay")
    token = _login(client).json()["access_token"]

    me = client.get("/v1/app/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "a@example.com"
    assert body["roles"] == ["student"]
    assert body["id"].startswith("usr_")


def test_me_never_exposes_the_password_hash_or_row_id(client):
    _register(client)
    token = _login(client).json()["access_token"]
    me = client.get("/v1/app/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert "hashed_password" not in me.text
    assert "legacy_id" not in me.text


def test_refresh_without_a_cookie_is_401(client):
    assert client.post("/v1/app/auth/refresh").status_code == 401


def test_refresh_with_the_cookie_returns_a_new_access_token(client):
    _register(client)
    _login(client)  # TestClient keeps cookies across calls
    refreshed = client.post("/v1/app/auth/refresh")

    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


def test_logout_returns_204(client):
    _register(client)
    _login(client)
    assert client.post("/v1/app/auth/logout").status_code == 204


def test_logout_clears_the_cookie(client):
    _register(client)
    _login(client)
    response = client.post("/v1/app/auth/logout")

    cookie = response.headers.get("set-cookie", "")
    assert "refresh_token=" in cookie
    assert 'max-age=0' in cookie.lower() or "expires=thu, 01 jan 1970" in cookie.lower()


def test_registration_sends_a_verification_email(client, notifier):
    _register(client)
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == "a@example.com"


def test_a_new_account_is_not_verified(client):
    assert _register(client).json()["email_verified"] is False


def test_verifying_with_the_emailed_token_marks_the_account_verified(client, notifier):
    _register(client)
    _, token = notifier.sent[0]

    response = client.post("/v1/app/auth/verify-email", json={"token": token})
    assert response.status_code == 200
    assert response.json()["email_verified"] is True


def test_verifying_with_a_bad_token_is_400(client):
    _register(client)
    response = client.post("/v1/app/auth/verify-email", json={"token": "nonsense"})
    assert response.status_code == 400


def test_an_unverified_user_can_still_sign_in(client):
    """Blocking login on verification locks people out when email is slow."""
    _register(client)
    assert _login(client).status_code == 200


def test_resend_verification_requires_a_token(client):
    assert client.post("/v1/app/auth/resend-verification").status_code == 401


def test_resend_verification_sends_another_email(client, notifier):
    _register(client)
    token = _login(client).json()["access_token"]

    response = client.post(
        "/v1/app/auth/resend-verification", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 202
    assert len(notifier.sent) == 2


def test_resend_answers_the_same_for_an_already_verified_account(client, notifier):
    _register(client)
    _, first_token = notifier.sent[0]
    client.post("/v1/app/auth/verify-email", json={"token": first_token})

    access = _login(client).json()["access_token"]
    response = client.post(
        "/v1/app/auth/resend-verification", headers={"Authorization": f"Bearer {access}"}
    )

    assert response.status_code == 202
    assert len(notifier.sent) == 1  # nothing new sent


# ── password reset ──────────────────────────────────────────────────────────


class ResetRecordingNotifier(RecordingNotifier):
    def __init__(self) -> None:
        super().__init__()
        self.resets: list[tuple[str, str]] = []

    def send_password_reset(self, *, email: str, token: str) -> None:
        self.resets.append((email, token))


@pytest.fixture
def reset_notifier() -> ResetRecordingNotifier:
    return ResetRecordingNotifier()


@pytest.fixture
def reset_client(db_session, reset_notifier) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: reset_notifier
    return TestClient(app, raise_server_exceptions=False)


def _forgot(client: TestClient, email: str = "a@example.com"):
    return client.post("/v1/app/auth/forgot-password", json={"email": email})


def test_forgot_password_accepts_a_known_address(reset_client):
    _register(reset_client)
    assert _forgot(reset_client).status_code == 202


def test_forgot_password_answers_the_same_for_an_unknown_address(reset_client):
    """Anything else makes this endpoint a free membership oracle."""
    _register(reset_client)
    known = _forgot(reset_client, "a@example.com")
    unknown = _forgot(reset_client, "nobody@example.com")

    assert known.status_code == unknown.status_code
    assert known.json() == unknown.json()


def test_only_a_known_address_actually_gets_an_email(reset_client, reset_notifier):
    _register(reset_client)
    _forgot(reset_client, "nobody@example.com")
    assert reset_notifier.resets == []

    _forgot(reset_client, "a@example.com")
    assert len(reset_notifier.resets) == 1


def test_resetting_with_the_emailed_token_works(reset_client, reset_notifier):
    _register(reset_client)
    _forgot(reset_client)
    _, token = reset_notifier.resets[0]

    response = reset_client.post(
        "/v1/app/auth/reset-password",
        json={"token": token, "password": "a brand new password"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_the_new_password_works_and_the_old_one_does_not(reset_client, reset_notifier):
    _register(reset_client)
    _forgot(reset_client)
    _, token = reset_notifier.resets[0]
    reset_client.post(
        "/v1/app/auth/reset-password",
        json={"token": token, "password": "a brand new password"},
    )

    assert _login(reset_client, password=PASSWORD).status_code == 401
    assert _login(reset_client, password="a brand new password").status_code == 200


def test_a_bad_reset_token_is_400(reset_client):
    response = reset_client.post(
        "/v1/app/auth/reset-password",
        json={"token": "nonsense", "password": "a brand new password"},
    )
    assert response.status_code == 400


def test_reset_rejects_a_short_password(reset_client, reset_notifier):
    _register(reset_client)
    _forgot(reset_client)
    _, token = reset_notifier.resets[0]

    response = reset_client.post(
        "/v1/app/auth/reset-password", json={"token": token, "password": "short"}
    )
    assert response.status_code == 422


# ── change password ─────────────────────────────────────────────────────────


def test_change_password_requires_a_token(client):
    response = client.post(
        "/v1/app/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "a brand new password"},
    )
    assert response.status_code == 401


def test_change_password_requires_the_current_one(client):
    _register(client)
    token = _login(client).json()["access_token"]

    response = client.post(
        "/v1/app/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "wrong", "new_password": "a brand new password"},
    )
    assert response.status_code == 401


def test_change_password_works_and_returns_a_fresh_token(client):
    _register(client)
    token = _login(client).json()["access_token"]

    response = client.post(
        "/v1/app/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": PASSWORD, "new_password": "a brand new password"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]

    assert _login(client, password="a brand new password").status_code == 200
