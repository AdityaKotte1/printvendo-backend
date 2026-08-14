from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY="x" * 32,
    SECRETS_ENCRYPTION_KEY="k" * 44,
    CORS_ORIGINS="http://localhost:3000",
)


def test_health_returns_ok():
    client = TestClient(create_app(SETTINGS))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_version_and_env():
    client = TestClient(create_app(SETTINGS))
    body = client.get("/health").json()
    assert body["version"]
    assert body["env"] == "dev"


def test_cors_allows_a_configured_origin():
    client = TestClient(create_app(SETTINGS))
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_does_not_allow_an_unconfigured_origin():
    client = TestClient(create_app(SETTINGS))
    response = client.get("/health", headers={"Origin": "http://evil.test"})
    assert "access-control-allow-origin" not in response.headers


def test_error_handlers_are_installed():
    app = create_app(SETTINGS)
    from app.core.errors import AppError

    assert AppError in app.exception_handlers
