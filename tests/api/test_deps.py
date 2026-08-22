from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, get_db, get_secret, require_role
from app.core.bus import mark_for_wake
from app.core.config import Settings
from app.core.errors import install_error_handlers
from app.core.ids import IdPrefix, new_id
from app.core.security import TokenType, create_token
from app.modules.identity import repository as repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

SECRET = "s" * 32


def _access(public_id: str) -> str:
    return create_token(public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5))


def _auth(public_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_access(public_id)}"}


@pytest.fixture
def student(db_session) -> User:
    user = User(email="s@example.test", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    repo.grant_role(db_session, user.id, Role.STUDENT)
    db_session.flush()
    return user


@pytest.fixture
def app(db_session) -> FastAPI:
    application = FastAPI()
    install_error_handlers(application)
    application.dependency_overrides[get_db] = lambda: db_session
    application.dependency_overrides[get_secret] = lambda: SECRET

    @application.get("/whoami")
    def whoami(user: CurrentUser) -> dict:
        return {"public_id": user.public_id}

    @application.get("/admin-only", dependencies=[Depends(require_role(Role.ADMIN))])
    def admin_only() -> dict:
        return {"ok": True}

    return application


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_a_valid_token_resolves_the_user(app, student):
    response = _client(app).get("/whoami", headers=_auth(student.public_id))
    assert response.status_code == 200
    assert response.json()["public_id"] == student.public_id


def test_a_missing_header_is_401(app):
    assert _client(app).get("/whoami").status_code == 401


def test_a_malformed_header_is_401(app):
    response = _client(app).get("/whoami", headers={"Authorization": "not-bearer"})
    assert response.status_code == 401


def test_a_bearer_header_with_no_token_is_401(app):
    response = _client(app).get("/whoami", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_a_garbage_token_is_401(app):
    response = _client(app).get("/whoami", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401


def test_a_refresh_token_is_not_accepted_as_an_access_token(app, student):
    token = create_token(student.public_id, TokenType.REFRESH, SECRET, timedelta(days=1))
    response = _client(app).get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_a_token_signed_with_another_secret_is_401(app, student):
    token = create_token(student.public_id, TokenType.ACCESS, "o" * 32, timedelta(minutes=5))
    response = _client(app).get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_an_expired_token_is_401(app, student):
    token = create_token(student.public_id, TokenType.ACCESS, SECRET, timedelta(seconds=-1))
    response = _client(app).get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_a_token_for_a_user_who_does_not_exist_is_401(app):
    response = _client(app).get("/whoami", headers=_auth(new_id(IdPrefix.USER)))
    assert response.status_code == 401


def test_a_token_carrying_a_kiosk_id_instead_of_a_user_id_is_401(app):
    """parse_id in the repository is what refuses this, not a database miss."""
    response = _client(app).get("/whoami", headers=_auth(new_id(IdPrefix.KIOSK)))
    assert response.status_code == 401


def test_an_inactive_user_is_401(app, db_session, student):
    student.is_active = False
    db_session.flush()
    response = _client(app).get("/whoami", headers=_auth(student.public_id))
    assert response.status_code == 401


def test_role_guard_refuses_a_user_without_the_role(app, student):
    response = _client(app).get("/admin-only", headers=_auth(student.public_id))
    assert response.status_code == 403


def test_role_guard_admits_a_user_with_the_role(app, db_session, student):
    repo.grant_role(db_session, student.id, Role.ADMIN)
    db_session.flush()

    response = _client(app).get("/admin-only", headers=_auth(student.public_id))
    assert response.status_code == 200


def test_role_guard_still_requires_a_token(app):
    """A guarded route must be 401 without credentials, not 403."""
    assert _client(app).get("/admin-only").status_code == 401


def test_errors_use_the_detail_envelope(app):
    body = _client(app).get("/whoami").json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], str)


# ── waking a kiosk after the transaction that gave it work ──────────────────


def _db_settings(postgres_url: str) -> Settings:
    return Settings(
        ENV="dev",
        DATABASE_URL=postgres_url,
        REDIS_URL="redis://localhost:6379/0",
        JWT_SECRET_KEY="x" * 32,
        SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
        CORS_ORIGINS="https://printvendo.com",
        PUBLIC_BASE_URL="https://api.printvendo.com",
    )


def test_a_committed_request_wakes_the_kiosks_it_queued_work_for(
    schema, postgres_url, monkeypatch
):
    """The wake fires from `get_db`, after the commit, so no route has to
    remember to send one. Driven through the real dependency rather than a
    stand-in, because the ordering -- commit first, publish second -- is the
    whole point and only this generator expresses it."""
    published: list[int] = []

    class Recording:
        def wake(self, kiosk_id: int) -> None:
            published.append(kiosk_id)

    monkeypatch.setattr("app.api.deps.redis_bus", lambda url: Recording())

    generator = get_db(_db_settings(postgres_url))
    session = next(generator)
    mark_for_wake(session, 41)
    with pytest.raises(StopIteration):
        next(generator)

    assert published == [41]


def test_a_failed_request_wakes_nobody(schema, postgres_url, monkeypatch):
    """The transaction rolled back, so there is no work to look at. A device
    told otherwise would spend its one notification finding nothing."""
    published: list[int] = []

    class Recording:
        def wake(self, kiosk_id: int) -> None:
            published.append(kiosk_id)

    monkeypatch.setattr("app.api.deps.redis_bus", lambda url: Recording())

    generator = get_db(_db_settings(postgres_url))
    session = next(generator)
    mark_for_wake(session, 41)
    with pytest.raises(RuntimeError):
        generator.throw(RuntimeError("the handler failed"))

    assert published == []
