"""What needs attention, and what was done.

Both tables existed in the backend being replaced and neither was in the path:
`notifications` had an admin flag nothing filtered on, and `admin_audit_log` was
written from 15 of 94 mutating routes and read by no route at all. Here they are
written; these are the routes that make them read.

The properties under test:

* an alert seen a thousand times is one row with a count, and the list says so;
* the audit trail names its actor as a person, without exposing a primary key;
* neither surface is reachable by anyone but an admin.
"""

from datetime import UTC, datetime, timedelta

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
from app.modules.ops import AlertSeverity, AuditEntry, audit, raise_alert

SECRET = "s" * 32
BOX_KEY = Fernet.generate_key().decode()

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
def admin(db_session) -> User:
    return _user(db_session, "ops@printvendo.com", Role.ADMIN)


@pytest.fixture
def admin_auth(admin) -> dict[str, str]:
    return _auth(admin)


@pytest.fixture
def owner_auth(db_session) -> dict[str, str]:
    return _auth(_user(db_session, "shopkeeper@example.com", Role.OWNER))


ALERTS = "/v1/admin/alerts"
AUDIT = "/v1/admin/audit"


# -- alerts -----------------------------------------------------------------


def test_open_alerts_come_back_worst_first(client, admin_auth, db_session):
    raise_alert(
        db_session,
        kind="kiosk.paper.low",
        severity=AlertSeverity.INFO,
        summary="Tray is getting low",
        dedupe_key="paper:ksk_1",
    )
    raise_alert(
        db_session,
        kind="kiosk.offline",
        severity=AlertSeverity.CRITICAL,
        summary="Kiosk has not checked in",
        dedupe_key="offline:ksk_1",
    )
    db_session.flush()

    listed = client.get(ALERTS, headers=admin_auth).json()

    assert [a["severity"] for a in listed] == ["critical", "info"]
    assert listed[0]["summary"] == "Kiosk has not checked in"
    assert listed[0]["id"].startswith("alr_")


def test_a_recurring_condition_is_one_row_with_a_count(client, admin_auth, db_session):
    """The rule that makes the list readable. A kiosk offline for a week is one
    alert seen 10,080 times -- a wall of identical rows is why nobody read the
    old backend's notifications."""
    for _ in range(3):
        raise_alert(
            db_session,
            kind="kiosk.offline",
            severity=AlertSeverity.WARNING,
            summary="Kiosk has not checked in",
            dedupe_key="offline:ksk_1",
        )
    db_session.flush()

    listed = client.get(ALERTS, headers=admin_auth).json()

    assert len(listed) == 1
    assert listed[0]["occurrences"] == 3


def test_alerts_can_be_narrowed_to_one_severity(client, admin_auth, db_session):
    raise_alert(
        db_session,
        kind="kiosk.paper.low",
        severity=AlertSeverity.INFO,
        summary="low",
        dedupe_key="paper:ksk_1",
    )
    raise_alert(
        db_session,
        kind="kiosk.offline",
        severity=AlertSeverity.CRITICAL,
        summary="offline",
        dedupe_key="offline:ksk_1",
    )
    db_session.flush()

    listed = client.get(f"{ALERTS}?severity=critical", headers=admin_auth).json()

    assert [a["kind"] for a in listed] == ["kiosk.offline"]


def test_a_nonsense_severity_is_refused_rather_than_ignored(client, admin_auth):
    """422, not a silently unfiltered list. An operator who filtered for
    "urgent" and got everything back would read it as "nothing is urgent"."""
    assert client.get(f"{ALERTS}?severity=urgent", headers=admin_auth).status_code == 422


def test_resolving_an_alert_takes_it_off_the_list(client, admin_auth, db_session):
    raise_alert(
        db_session,
        kind="kiosk.offline",
        severity=AlertSeverity.CRITICAL,
        summary="offline",
        dedupe_key="offline:ksk_1",
    )
    db_session.flush()
    alert_id = client.get(ALERTS, headers=admin_auth).json()[0]["id"]

    resolved = client.post(f"{ALERTS}/{alert_id}/resolve", headers=admin_auth)

    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True
    assert client.get(ALERTS, headers=admin_auth).json() == []


def test_resolving_the_same_alert_twice_is_not_an_error(client, admin_auth, db_session):
    """Two admins clearing the same list at once is ordinary, not a conflict."""
    raise_alert(
        db_session,
        kind="kiosk.offline",
        severity=AlertSeverity.CRITICAL,
        summary="offline",
        dedupe_key="offline:ksk_1",
    )
    db_session.flush()
    alert_id = client.get(ALERTS, headers=admin_auth).json()[0]["id"]

    client.post(f"{ALERTS}/{alert_id}/resolve", headers=admin_auth)
    second = client.post(f"{ALERTS}/{alert_id}/resolve", headers=admin_auth)

    assert second.status_code == 200


def test_an_unknown_alert_is_not_found(client, admin_auth):
    response = client.post(f"{ALERTS}/alr_0000000000000000/resolve", headers=admin_auth)

    assert response.status_code == 404


# -- the audit trail --------------------------------------------------------


def test_the_trail_names_its_actor_as_a_person(client, admin_auth, admin, db_session):
    audit.record(
        db_session,
        action="kiosk.pricing.update",
        entity_type="kiosk",
        entity_id="ksk_abc",
        actor_user_id=admin.id,
        before={"bw_single": "2.00"},
        after={"bw_single": "3.00"},
    )
    db_session.flush()

    entry = client.get(AUDIT, headers=admin_auth).json()[0]

    assert entry["actor_id"] == admin.public_id
    assert entry["actor_email"] == admin.email
    assert entry["action"] == "kiosk.pricing.update"
    assert entry["before"] == {"bw_single": "2.00"}
    assert entry["after"] == {"bw_single": "3.00"}


def test_the_trail_never_carries_a_row_id(client, admin_auth, admin, db_session):
    audit.record(
        db_session,
        action="kiosk.pricing.update",
        entity_type="kiosk",
        entity_id="ksk_abc",
        actor_user_id=admin.id,
    )
    db_session.flush()

    entry = client.get(AUDIT, headers=admin_auth).json()[0]

    assert "actor_user_id" not in entry
    assert entry["actor_id"] != str(admin.id)


def test_a_system_action_has_no_actor_rather_than_a_missing_one(
    client, admin_auth, db_session
):
    """"Nobody did this" is a real distinction from "we failed to record who",
    and the response keeps it: null, not an empty string."""
    audit.record(
        db_session,
        action="order.expired",
        entity_type="order",
        entity_id="ord_abc",
        actor_user_id=None,
    )
    db_session.flush()

    entry = client.get(AUDIT, headers=admin_auth).json()[0]

    assert entry["actor_id"] is None
    assert entry["actor_email"] is None


def test_the_trail_can_be_narrowed_to_one_entity(client, admin_auth, admin, db_session):
    """The question an admin console asks constantly: what has happened to this
    kiosk."""
    audit.record(
        db_session,
        action="kiosk.pricing.update",
        entity_type="kiosk",
        entity_id="ksk_one",
        actor_user_id=admin.id,
    )
    audit.record(
        db_session,
        action="kiosk.pricing.update",
        entity_type="kiosk",
        entity_id="ksk_two",
        actor_user_id=admin.id,
    )
    db_session.flush()

    listed = client.get(
        f"{AUDIT}?entity_type=kiosk&entity_id=ksk_one", headers=admin_auth
    ).json()

    assert [e["entity_id"] for e in listed] == ["ksk_one"]


def test_the_trail_can_be_narrowed_to_one_action(client, admin_auth, admin, db_session):
    audit.record(
        db_session,
        action="payment_config.keys.set",
        entity_type="user",
        entity_id="usr_one",
        actor_user_id=admin.id,
    )
    audit.record(
        db_session,
        action="kiosk.pricing.update",
        entity_type="kiosk",
        entity_id="ksk_one",
        actor_user_id=admin.id,
    )
    db_session.flush()

    listed = client.get(
        f"{AUDIT}?action=payment_config.keys.set", headers=admin_auth
    ).json()

    assert [e["action"] for e in listed] == ["payment_config.keys.set"]


def test_the_trail_is_newest_first(client, admin_auth, admin, db_session):
    older = datetime.now(UTC) - timedelta(hours=1)
    audit.record(
        db_session, action="a.first", entity_type="kiosk", actor_user_id=admin.id
    )
    db_session.flush()
    db_session.query(AuditEntry).update({"created_at": older})
    audit.record(
        db_session, action="b.second", entity_type="kiosk", actor_user_id=admin.id
    )
    db_session.flush()

    listed = client.get(AUDIT, headers=admin_auth).json()

    assert [e["action"] for e in listed] == ["b.second", "a.first"]


# -- who may read this ------------------------------------------------------


def test_neither_surface_is_reachable_by_an_owner(client, owner_auth):
    """An owner reading the audit trail would see every other shop's prices,
    staff changes and payment configuration."""
    assert client.get(ALERTS, headers=owner_auth).status_code == 403
    assert client.get(AUDIT, headers=owner_auth).status_code == 403


def test_neither_surface_is_reachable_without_a_token(client):
    assert client.get(ALERTS).status_code == 401
    assert client.get(AUDIT).status_code == 401
