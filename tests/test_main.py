from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY="x" * 32,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="http://localhost:3000",
)


def _reachable(postgres_url: str) -> Settings:
    """Settings pointed at the database the tests actually run against."""
    return SETTINGS.model_copy(update={"DATABASE_URL": postgres_url})


def test_health_returns_ok_when_the_database_answers(postgres_url):
    client = TestClient(create_app(_reachable(postgres_url)))

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "ok"


def test_health_reports_version_and_env(postgres_url):
    body = TestClient(create_app(_reachable(postgres_url))).get("/health").json()

    assert body["version"]
    assert body["env"] == "dev"


def test_health_is_unhealthy_when_the_database_is_not_there():
    """A probe that answers 200 while nothing works is worse than no probe.

    SETTINGS names a database that does not exist, which is the whole point:
    this is the case the old `/health` could not tell apart from a working one,
    so a load balancer kept sending traffic to a process that could not serve a
    single request.
    """
    client = TestClient(create_app(SETTINGS))

    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "unreachable"


def test_an_unhealthy_probe_still_says_which_version_is_unhealthy(postgres_url):
    body = TestClient(create_app(SETTINGS)).get("/health").json()

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


def test_app_loggers_are_audible_in_dev(caplog):
    """LoggingNotifier is useless if app.* logs are swallowed.

    uvicorn only configures its own loggers, so without explicit setup the root
    logger's WARNING default discards everything the application logs -- and the
    local email-verification flow becomes impossible to complete.
    """
    import logging

    create_app(SETTINGS)
    assert logging.getLogger("app").level == logging.INFO

    with caplog.at_level(logging.INFO, logger="app.core.notifier"):
        from app.core.notifier import LoggingNotifier

        LoggingNotifier().send_email_verification(email="a@example.com", token="tok123")

    assert "tok123" in caplog.text


def test_app_loggers_are_quiet_outside_dev():
    """Verification tokens are secrets and must not reach a production log."""
    import logging

    prod = SETTINGS.model_copy(update={"ENV": "staging"})
    create_app(prod)
    assert logging.getLogger("app").level == logging.WARNING
