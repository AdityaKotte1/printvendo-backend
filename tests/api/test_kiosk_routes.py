"""Scope isolation at the HTTP layer.

The service-level tests prove `kiosk_scope` and the repository behave. These
prove the wiring: that a real request from one owner cannot reach another
owner's kiosk, and that a refiller's responses carry no money.
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
from app.modules.kiosks.enums import AssignmentRole
from app.modules.kiosks.models import KioskAssignment
from app.modules.kiosks.registry import create_kiosk

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="http://localhost:3000",
)


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    return TestClient(app, raise_server_exceptions=False)


def _auth(user: User) -> dict[str, str]:
    token = create_token(user.public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


def _user(db_session, email: str, *roles: Role) -> User:
    user = User(email=email, hashed_password="x")
    db_session.add(user)
    db_session.flush()
    for role in roles:
        identity_repo.grant_role(db_session, user.id, role)
    db_session.flush()
    return user


def _kiosk_for(db_session, user: User, name: str, role=AssignmentRole.OWNER):
    kiosk = create_kiosk(db_session, name=name)
    db_session.flush()
    db_session.add(KioskAssignment(kiosk_id=kiosk.id, user_id=user.id, role=role))
    db_session.flush()
    return kiosk


@pytest.fixture
def alice(db_session) -> User:
    return _user(db_session, "alice@example.com", Role.OWNER)


@pytest.fixture
def bob(db_session) -> User:
    return _user(db_session, "bob@example.com", Role.OWNER)


# ── the isolation that matters ──────────────────────────────────────────────


def test_an_owner_lists_only_their_own_kiosks(client, db_session, alice, bob):
    _kiosk_for(db_session, alice, "Alice Shop")
    _kiosk_for(db_session, bob, "Bob Shop")

    response = client.get("/v1/owner/kiosks", headers=_auth(alice))
    assert response.status_code == 200
    assert [k["name"] for k in response.json()] == ["Alice Shop"]


def test_an_owner_gets_404_for_another_owners_kiosk(client, db_session, alice, bob):
    """404 not 403: a 403 confirms the kiosk exists, which is itself a
    disclosure about a competitor's estate."""
    _kiosk_for(db_session, alice, "Alice Shop")
    theirs = _kiosk_for(db_session, bob, "Bob Shop")

    response = client.get(f"/v1/owner/kiosks/{theirs.public_id}", headers=_auth(alice))
    assert response.status_code == 404


def test_the_404_is_worded_identically_to_a_kiosk_that_never_existed(
    client, db_session, alice, bob
):
    _kiosk_for(db_session, alice, "Alice Shop")
    theirs = _kiosk_for(db_session, bob, "Bob Shop")

    other = client.get(f"/v1/owner/kiosks/{theirs.public_id}", headers=_auth(alice))
    absent = client.get("/v1/owner/kiosks/ksk_0000000000000000", headers=_auth(alice))

    assert other.json() == absent.json()


def test_an_owner_cannot_reprice_another_owners_kiosk(client, db_session, alice, bob):
    theirs = _kiosk_for(db_session, bob, "Bob Shop")

    response = client.put(
        f"/v1/owner/kiosks/{theirs.public_id}/pricing",
        headers=_auth(alice),
        json={"bw_single": "1.00"},
    )
    assert response.status_code == 404


def test_an_owner_cannot_refill_another_owners_kiosk(client, db_session, alice, bob):
    theirs = _kiosk_for(db_session, bob, "Bob Shop")

    response = client.post(
        f"/v1/owner/kiosks/{theirs.public_id}/paper/reset", headers=_auth(alice)
    )
    assert response.status_code == 404


def test_an_owner_cannot_read_another_owners_staff(client, db_session, alice, bob):
    theirs = _kiosk_for(db_session, bob, "Bob Shop")

    response = client.get(
        f"/v1/owner/kiosks/{theirs.public_id}/staff", headers=_auth(alice)
    )
    assert response.status_code == 404


def test_an_admin_sees_every_kiosk_through_the_same_routes(client, db_session, alice, bob):
    """Admin is a wider scope, not a parallel router."""
    _kiosk_for(db_session, alice, "Alice Shop")
    _kiosk_for(db_session, bob, "Bob Shop")
    admin = _user(db_session, "admin@example.com", Role.ADMIN)

    response = client.get("/v1/owner/kiosks", headers=_auth(admin))
    assert sorted(k["name"] for k in response.json()) == ["Alice Shop", "Bob Shop"]


def test_a_student_sees_no_kiosks(client, db_session):
    student = _user(db_session, "student@example.com", Role.STUDENT)
    _kiosk_for(db_session, _user(db_session, "o@example.com", Role.OWNER), "A Shop")

    response = client.get("/v1/owner/kiosks", headers=_auth(student))
    assert response.status_code == 200
    assert response.json() == []


def test_kiosk_routes_require_a_token(client):
    assert client.get("/v1/owner/kiosks").status_code == 401


# ── owner operations ────────────────────────────────────────────────────────


def test_an_owner_can_read_and_set_pricing(client, db_session, alice):
    mine = _kiosk_for(db_session, alice, "Alice Shop")

    read = client.get(f"/v1/owner/kiosks/{mine.public_id}/pricing", headers=_auth(alice))
    assert read.status_code == 200
    assert "prices" in read.json() and "band" in read.json()

    written = client.put(
        f"/v1/owner/kiosks/{mine.public_id}/pricing",
        headers=_auth(alice),
        json={"bw_single": "3.00", "bw_double": "5.00"},
    )
    assert written.status_code == 200
    assert written.json()["prices"]["bw_single"] == "3.00"


def test_pricing_refuses_a_duplex_price_below_single(client, db_session, alice):
    mine = _kiosk_for(db_session, alice, "Alice Shop")

    response = client.put(
        f"/v1/owner/kiosks/{mine.public_id}/pricing",
        headers=_auth(alice),
        json={"bw_single": "5.00", "bw_double": "3.00"},
    )
    assert response.status_code == 400


def test_an_owner_can_refill_their_own_kiosk(client, db_session, alice):
    mine = _kiosk_for(db_session, alice, "Alice Shop")

    client.put(
        f"/v1/owner/kiosks/{mine.public_id}/paper",
        headers=_auth(alice),
        json={"sheets_left": 10},
    )
    response = client.post(
        f"/v1/owner/kiosks/{mine.public_id}/paper/reset", headers=_auth(alice)
    )

    assert response.status_code == 200
    assert response.json()["sheets_remaining"] == 250


def test_an_owner_cannot_set_an_arbitrary_onboarding_stage(client, db_session, alice):
    """The onboarding ladder is not an owner's to climb."""
    mine = _kiosk_for(db_session, alice, "Alice Shop")

    response = client.post(
        f"/v1/owner/kiosks/{mine.public_id}/status",
        headers=_auth(alice),
        json={"stage": "approved"},
    )
    assert response.status_code == 400


def test_an_owner_cannot_invent_a_stage(client, db_session, alice):
    mine = _kiosk_for(db_session, alice, "Alice Shop")

    response = client.post(
        f"/v1/owner/kiosks/{mine.public_id}/status",
        headers=_auth(alice),
        json={"stage": "banana"},
    )
    assert response.status_code == 400


def test_inviting_answers_the_same_for_known_and_unknown_addresses(
    client, db_session, alice
):
    mine = _kiosk_for(db_session, alice, "Alice Shop")
    _user(db_session, "exists@example.com", Role.REFILLER)

    known = client.post(
        f"/v1/owner/kiosks/{mine.public_id}/staff/invite",
        headers=_auth(alice),
        json={"email": "exists@example.com"},
    )
    unknown = client.post(
        f"/v1/owner/kiosks/{mine.public_id}/staff/invite",
        headers=_auth(alice),
        json={"email": "nobody@example.com"},
    )

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


def test_inviting_binds_nobody_until_accepted(client, db_session, alice):
    mine = _kiosk_for(db_session, alice, "Alice Shop")
    _user(db_session, "them@example.com", Role.REFILLER)

    client.post(
        f"/v1/owner/kiosks/{mine.public_id}/staff/invite",
        headers=_auth(alice),
        json={"email": "them@example.com"},
    )

    # The owner themselves is attached, so the list is not empty -- what must
    # be absent is the person who was merely invited.
    staff = client.get(f"/v1/owner/kiosks/{mine.public_id}/staff", headers=_auth(alice))
    emails = [member["email"] for member in staff.json()]

    assert "them@example.com" not in emails
    assert emails == ["alice@example.com"]


def test_removing_staff_who_do_not_work_here_is_404(client, db_session, alice):
    """Naming any account on the platform must not reveal whether it exists."""
    mine = _kiosk_for(db_session, alice, "Alice Shop")
    stranger = _user(db_session, "stranger@example.com", Role.REFILLER)

    response = client.delete(
        f"/v1/owner/kiosks/{mine.public_id}/staff/{stranger.public_id}",
        headers=_auth(alice),
    )
    assert response.status_code == 404


# ── refiller: paper only ────────────────────────────────────────────────────


@pytest.fixture
def refiller(db_session) -> User:
    return _user(db_session, "refiller@example.com", Role.REFILLER)


def test_a_refiller_sees_only_the_kiosks_they_cover(client, db_session, refiller, alice):
    _kiosk_for(db_session, alice, "Not Mine")
    covered = create_kiosk(db_session, name="My Round")
    db_session.flush()
    db_session.add(
        KioskAssignment(
            kiosk_id=covered.id, user_id=refiller.id, role=AssignmentRole.REFILLER
        )
    )
    db_session.flush()

    response = client.get("/v1/refiller/kiosks", headers=_auth(refiller))
    assert [k["name"] for k in response.json()] == ["My Round"]


def test_a_refiller_response_carries_no_money(client, db_session, refiller):
    """Enforced by the response type having no price field to populate."""
    covered = _kiosk_for(db_session, refiller, "My Round", AssignmentRole.REFILLER)

    body = client.get(
        f"/v1/refiller/kiosks/{covered.public_id}", headers=_auth(refiller)
    ).text

    for forbidden in ("price", "bw_single", "earnings", "accepts_wallet", "kiosk_type"):
        assert forbidden not in body, forbidden


def test_a_refiller_can_refill(client, db_session, refiller):
    covered = _kiosk_for(db_session, refiller, "My Round", AssignmentRole.REFILLER)

    response = client.post(
        f"/v1/refiller/kiosks/{covered.public_id}/paper/reset", headers=_auth(refiller)
    )
    assert response.status_code == 200
    assert response.json()["sheets_remaining"] == 250


def test_a_refiller_can_report_an_empty_tray(client, db_session, refiller):
    covered = _kiosk_for(db_session, refiller, "My Round", AssignmentRole.REFILLER)

    response = client.post(
        f"/v1/refiller/kiosks/{covered.public_id}/paper/out-of-paper",
        headers=_auth(refiller),
    )
    assert response.json()["sheets_remaining"] == 0


def test_a_refiller_cannot_reach_another_rounds_kiosk(client, db_session, refiller, alice):
    theirs = _kiosk_for(db_session, alice, "Not Mine")

    response = client.get(
        f"/v1/refiller/kiosks/{theirs.public_id}", headers=_auth(refiller)
    )
    assert response.status_code == 404


def test_refill_logs_record_who_refilled(client, db_session, refiller):
    covered = _kiosk_for(db_session, refiller, "My Round", AssignmentRole.REFILLER)

    client.post(
        f"/v1/refiller/kiosks/{covered.public_id}/paper/reset", headers=_auth(refiller)
    )
    logs = client.get(
        f"/v1/refiller/kiosks/{covered.public_id}/refill-logs", headers=_auth(refiller)
    )

    assert logs.status_code == 200
    assert len(logs.json()) == 1
