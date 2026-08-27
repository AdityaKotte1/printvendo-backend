"""Restarting the machine in a shop, and hearing that it cannot print.

Two properties beyond the service's own. That **one route serves the owner and
the admin** — the backend being replaced had this twice, once per audience, and
the copies drifted. And that a stuck printer **closes the shop to students
only**: the kiosk stops selling while every operator surface still shows it and
says why.
"""

from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret
from app.core.config import Settings
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks import is_selling
from app.modules.kiosks.devices import register_device
from app.modules.kiosks.enums import (
    AssignmentRole,
    DeviceCommandState,
    KioskType,
    OnboardingStage,
)
from app.modules.kiosks.models import Kiosk, KioskAssignment, KioskPaper

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="https://owner.printvendo.com",
)


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
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
def an_admin(db_session) -> User:
    return _user(db_session, "operator@example.com", Role.ADMIN)


@pytest.fixture
def a_student(db_session) -> User:
    return _user(db_session, "student@example.com", Role.STUDENT)


@pytest.fixture
def kiosk(db_session, owner) -> Kiosk:
    kiosk = Kiosk(
        name="Restart Test Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
        accepts_wallet=True,
    )
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id, capacity=500, used=0))
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=owner.id, role=AssignmentRole.OWNER)
    )
    db_session.flush()
    return kiosk


@pytest.fixture
def device_token(db_session, kiosk) -> str:
    from app.modules.kiosks.devices import issue_enrolment_code

    code = issue_enrolment_code(db_session, kiosk, created_by_user_id=None).code
    return register_device(db_session, code, device_key="restart-test").token


def _device(token: str) -> dict[str, str]:
    return {"X-Device-Token": token}


# ── asking ──────────────────────────────────────────────────────────────────


def test_an_owner_can_restart_the_agent(client, kiosk, owner, device_token):
    response = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "restart_agent"},
        headers=_auth(owner),
    )

    assert response.status_code == 200
    assert response.json()["command"] == "restart_agent"
    assert response.json()["state"] == "queued"


def test_an_admin_can_restart_any_shops_printing(client, kiosk, an_admin, device_token):
    """Admin is a wider scope through the same route, not a second router."""
    response = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "restart_printing"},
        headers=_auth(an_admin),
    )

    assert response.status_code == 200
    assert response.json()["command"] == "restart_printing"


def test_a_student_cannot(client, kiosk, a_student, device_token):
    response = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "restart_agent"},
        headers=_auth(a_student),
    )

    assert response.status_code == 403


def test_somebody_elses_kiosk_is_not_found(client, kiosk, db_session, device_token):
    stranger = _user(db_session, "other.shop@example.com", Role.OWNER)

    response = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "restart_agent"},
        headers=_auth(stranger),
    )

    assert response.status_code == 404


def test_a_kiosk_with_no_machine_says_so(client, kiosk, owner):
    response = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "restart_agent"},
        headers=_auth(owner),
    )

    assert response.status_code == 400
    assert "enrolled" in response.json()["detail"]


def test_an_unknown_command_is_refused(client, kiosk, owner, device_token):
    response = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "rm -rf /"},
        headers=_auth(owner),
    )

    assert response.status_code == 422


def test_the_history_shows_what_was_asked(client, kiosk, owner, device_token):
    client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "restart_printing"},
        headers=_auth(owner),
    )

    response = client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands", headers=_auth(owner)
    )

    assert response.status_code == 200
    assert [c["command"] for c in response.json()] == ["restart_printing"]


# ── the machine's side ──────────────────────────────────────────────────────


def test_the_machine_claims_what_was_asked(client, kiosk, owner, device_token):
    client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "restart_printing"},
        headers=_auth(owner),
    )

    response = client.post("/v1/device/commands/next", headers=_device(device_token))

    assert response.status_code == 200
    assert [c["command"] for c in response.json()] == ["restart_printing"]


def test_a_machine_with_no_token_is_refused(client):
    assert client.post("/v1/device/commands/next").status_code == 401


def test_the_machine_says_how_it_went(client, kiosk, owner, device_token):
    asked = client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/device/commands",
        json={"command": "restart_printing"},
        headers=_auth(owner),
    ).json()
    client.post("/v1/device/commands/next", headers=_device(device_token))

    response = client.post(
        f"/v1/device/commands/{asked['id']}/result",
        json={"succeeded": False, "error_message": "cups is not installed"},
        headers=_device(device_token),
    )

    assert response.status_code == 200
    assert response.json()["state"] == DeviceCommandState.FAILED.value
    assert response.json()["error_message"] == "cups is not installed"


# ── a stuck printer closes the shop ─────────────────────────────────────────


def test_a_stuck_printer_stops_the_shop_selling(client, db_session, kiosk, device_token):
    response = client.post(
        "/v1/device/printer-health",
        json={"stuck": True, "detail": "the queue has not moved in ten minutes"},
        headers=_device(device_token),
    )

    assert response.status_code == 204
    db_session.refresh(kiosk)
    assert kiosk.onboarding_stage is OnboardingStage.MAINTENANCE
    assert is_selling(kiosk) is False


def test_the_shop_is_still_visible_to_its_owner(
    client, db_session, kiosk, owner, device_token
):
    """Closed to students, not hidden from the person who has to fix it."""
    client.post(
        "/v1/device/printer-health",
        json={"stuck": True},
        headers=_device(device_token),
    )

    response = client.get("/v1/owner/kiosks", headers=_auth(owner))

    assert response.status_code == 200
    listed = {k["id"]: k for k in response.json()}
    assert kiosk.public_id in listed
    assert listed[kiosk.public_id]["onboarding_stage"] == "maintenance"
    assert listed[kiosk.public_id]["is_selling"] is False


def test_a_student_is_not_offered_a_stuck_shop(
    client, db_session, kiosk, a_student, device_token
):
    client.post(
        "/v1/device/printer-health",
        json={"stuck": True},
        headers=_device(device_token),
    )

    response = client.get("/v1/app/kiosks", headers=_auth(a_student))

    assert response.status_code == 200
    assert kiosk.public_id not in {k["id"] for k in response.json()}


def test_it_reopens_when_printing_works_again(client, db_session, kiosk, device_token):
    client.post(
        "/v1/device/printer-health", json={"stuck": True}, headers=_device(device_token)
    )

    response = client.post(
        "/v1/device/printer-health",
        json={"stuck": False},
        headers=_device(device_token),
    )

    assert response.status_code == 204
    db_session.refresh(kiosk)
    assert kiosk.onboarding_stage is OnboardingStage.LIVE
    assert is_selling(kiosk) is True


def test_a_stuck_printer_raises_an_alert_an_admin_can_see(
    client, kiosk, an_admin, device_token
):
    client.post(
        "/v1/device/printer-health", json={"stuck": True}, headers=_device(device_token)
    )

    response = client.get("/v1/admin/alerts", headers=_auth(an_admin))

    assert response.status_code == 200
    stuck = [a for a in response.json() if a["kind"] == "kiosk.printer_stuck"]
    assert len(stuck) == 1
    assert stuck[0]["resolved"] is False


def test_the_alert_stands_down_by_itself(client, kiosk, an_admin, device_token):
    """"It stopped on its own" is a different fact from "somebody dealt with
    it", and the console must not fill with shops that were briefly stuck."""
    client.post(
        "/v1/device/printer-health", json={"stuck": True}, headers=_device(device_token)
    )
    client.post(
        "/v1/device/printer-health", json={"stuck": False}, headers=_device(device_token)
    )

    response = client.get("/v1/admin/alerts", headers=_auth(an_admin))

    # `/v1/admin/alerts` lists what is still open, so standing down is the
    # alert leaving the list -- not a resolved row sitting in it.
    assert [a for a in response.json() if a["kind"] == "kiosk.printer_stuck"] == []
