import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    install_error_handlers,
)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/notfound")
    def _notfound():
        raise NotFound("Kiosk not found")

    @app.get("/forbidden")
    def _forbidden():
        raise Forbidden("You do not have access to this kiosk")

    @app.get("/conflict")
    def _conflict():
        raise Conflict("That refiller is already assigned")

    @app.get("/boom")
    def _boom():
        raise RuntimeError("internal detail that must not leak")

    return TestClient(app, raise_server_exceptions=False)


def test_not_found_returns_404_with_detail(client):
    response = client.get("/notfound")
    assert response.status_code == 404
    assert response.json() == {"detail": "Kiosk not found"}


def test_forbidden_returns_403_with_detail(client):
    response = client.get("/forbidden")
    assert response.status_code == 403
    assert response.json() == {"detail": "You do not have access to this kiosk"}


def test_conflict_returns_409_with_detail(client):
    response = client.get("/conflict")
    assert response.status_code == 409


def test_unexpected_error_returns_500_without_leaking_internals(client):
    response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Something went wrong. Please try again."}
    assert "internal detail" not in response.text


def test_app_error_is_the_common_base():
    assert issubclass(NotFound, AppError)
    assert issubclass(Forbidden, AppError)
    assert issubclass(Conflict, AppError)
