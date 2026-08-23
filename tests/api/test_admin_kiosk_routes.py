"""Putting a kiosk into the world, and moving it along.

Until this router existed, a kiosk could not be created at all: the registry and
the onboarding ladder were built and tested, and the only way to reach either
was to write a row by hand. Everything an owner can do assumes a kiosk already
exists, and nothing made one.

The properties under test are the ones the stage machine exists for:

* the ladder cannot be skipped -- CONFIGURED is where an owned kiosk's Razorpay
  keys are confirmed, and jumping it is how a shop starts collecting student
  money into an account nobody has checked;
* a kiosk becoming owner-gateway drops out of service until it earns LIVE again;
* an owner is invited, not assigned, and the answer is the same either way.
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
from app.modules.kiosks import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk
from app.modules.ops import entries_for

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

KIOSKS = "/v1/admin/kiosks"


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


def _create(client, auth, **overrides):
    body = {"name": "Library Ground Floor"} | overrides
    return client.post(KIOSKS, headers=auth, json=body)


def _stage(client, auth, kiosk_id, stage, **extra):
    return client.post(
        f"{KIOSKS}/{kiosk_id}/stage", headers=auth, json={"stage": stage} | extra
    )


# -- creating one -----------------------------------------------------------


def test_a_new_kiosk_starts_registered_and_unlisted(client, admin_auth):
    created = _create(client, admin_auth)

    assert created.status_code == 201
    body = created.json()
    assert body["id"].startswith("ksk_")
    assert body["onboarding_stage"] == "registered"
    assert body["kiosk_type"] == "platform"
    assert body["owner_id"] is None


def test_a_new_kiosk_has_a_paper_tray_from_the_start(client, admin_auth, db_session):
    """Nothing downstream should have to ask whether a tray row exists before
    reading how much paper is left.

    A new tray is full rather than empty: paper is stored as sheets *used*
    against a capacity, so zero used is a fresh ream, and a kiosk installed with
    paper in it should not have to be "refilled" before it can print.
    """
    created = _create(client, admin_auth, paper_capacity=500)

    assert created.json()["paper"]["capacity"] == 500
    assert created.json()["paper"]["sheets_remaining"] == 500


def test_two_kiosks_cannot_share_a_name(client, admin_auth):
    """Names are how staff and refillers tell shops apart in a list."""
    _create(client, admin_auth)

    assert _create(client, admin_auth).status_code == 409


def test_a_kiosk_needs_a_name(client, admin_auth):
    assert _create(client, admin_auth, name="   ").status_code == 400


# -- the ladder -------------------------------------------------------------


def test_an_admin_climbs_the_ladder_one_rung_at_a_time(client, admin_auth):
    kiosk_id = _create(client, admin_auth).json()["id"]

    assert _stage(client, admin_auth, kiosk_id, "approved").status_code == 200
    assert _stage(client, admin_auth, kiosk_id, "configured").status_code == 200
    live = _stage(client, admin_auth, kiosk_id, "live")

    assert live.status_code == 200
    assert live.json()["onboarding_stage"] == "live"
    assert live.json()["is_selling"] is True


def test_the_configured_rung_cannot_be_skipped(client, admin_auth):
    """That step is where an owned kiosk's Razorpay keys and subscription are
    confirmed. Skipping it is how a shop starts taking student money into an
    account nobody has checked."""
    kiosk_id = _create(client, admin_auth).json()["id"]
    _stage(client, admin_auth, kiosk_id, "approved")

    skipped = _stage(client, admin_auth, kiosk_id, "live")

    assert skipped.status_code == 409
    assert "approved" in skipped.json()["detail"]


def test_a_stage_that_is_not_a_stage_is_refused(client, admin_auth):
    kiosk_id = _create(client, admin_auth).json()["id"]

    refused = _stage(client, admin_auth, kiosk_id, "definitely-live")

    assert refused.status_code == 400
    assert "definitely-live" in refused.json()["detail"]


def test_a_note_explains_a_stage_to_whoever_reads_it_next(client, admin_auth):
    kiosk_id = _create(client, admin_auth).json()["id"]

    moved = _stage(
        client, admin_auth, kiosk_id, "approved", note="hardware delivered 14 Aug"
    )

    assert moved.json()["onboarding_note"] == "hardware delivered 14 Aug"


def test_a_sold_kiosk_cannot_go_live_without_a_paid_up_owner(
    client, admin_auth, db_session
):
    """The payment gate, reached through the stage route. A SOLD kiosk whose
    owner cannot collect has nowhere for a student's money to land."""
    kiosk_id = _create(client, admin_auth).json()["id"]
    client.put(f"{KIOSKS}/{kiosk_id}/type", headers=admin_auth, json={"type": "sold"})
    _stage(client, admin_auth, kiosk_id, "approved")
    _stage(client, admin_auth, kiosk_id, "configured")

    refused = _stage(client, admin_auth, kiosk_id, "live")

    assert refused.status_code == 400
    assert db_session.query(Kiosk).one().onboarding_stage is OnboardingStage.CONFIGURED


# -- changing what a kiosk is -----------------------------------------------


def test_becoming_owner_gateway_takes_a_live_kiosk_out_of_service(
    client, admin_auth, db_session
):
    """Not a relabelling: it changes whose Razorpay collects. Left live, the
    next student's money would go to an account that may not exist."""
    kiosk_id = _create(client, admin_auth).json()["id"]
    for stage in ("approved", "configured", "live"):
        _stage(client, admin_auth, kiosk_id, stage)

    changed = client.put(
        f"{KIOSKS}/{kiosk_id}/type", headers=admin_auth, json={"type": "saas"}
    )

    assert changed.status_code == 200
    assert changed.json()["kiosk_type"] == "saas"
    assert changed.json()["onboarding_stage"] == "approved"
    assert "verified again" in changed.json()["onboarding_note"]


def test_becoming_a_platform_kiosk_keeps_it_selling(client, admin_auth):
    """Safe in place: the platform's own keys always work, so nothing is left
    uncollected."""
    kiosk_id = _create(client, admin_auth).json()["id"]
    client.put(f"{KIOSKS}/{kiosk_id}/type", headers=admin_auth, json={"type": "sold"})
    back = client.put(
        f"{KIOSKS}/{kiosk_id}/type", headers=admin_auth, json={"type": "platform"}
    )

    assert back.json()["kiosk_type"] == "platform"


def test_a_type_that_is_not_a_type_is_refused(client, admin_auth):
    kiosk_id = _create(client, admin_auth).json()["id"]

    refused = client.put(
        f"{KIOSKS}/{kiosk_id}/type", headers=admin_auth, json={"type": "franchise"}
    )

    assert refused.status_code == 400


# -- giving it an owner -----------------------------------------------------


def test_an_owner_is_invited_rather_than_assigned(client, admin_auth, db_session):
    """Consent, not administration. Until they accept, nobody is bound to the
    kiosk and no name or detail about them is disclosed."""
    kiosk_id = _create(client, admin_auth).json()["id"]

    invited = client.post(
        f"{KIOSKS}/{kiosk_id}/owner",
        headers=admin_auth,
        json={"email": "newshop@example.com"},
    )

    assert invited.status_code == 202
    assert client.get(f"{KIOSKS}/{kiosk_id}", headers=admin_auth).json()["owner_id"] is None


def test_inviting_an_owner_answers_the_same_either_way(client, admin_auth, db_session):
    """The enumeration oracle this platform's invites exist to avoid: an admin
    console is not a directory lookup for whether an address has an account."""
    _user(db_session, "existing@example.com", Role.OWNER)
    kiosk_id = _create(client, admin_auth).json()["id"]
    second_id = _create(client, admin_auth, name="Second Shop").json()["id"]

    known = client.post(
        f"{KIOSKS}/{kiosk_id}/owner",
        headers=admin_auth,
        json={"email": "existing@example.com"},
    )
    unknown = client.post(
        f"{KIOSKS}/{second_id}/owner",
        headers=admin_auth,
        json={"email": "nobody@example.com"},
    )

    assert known.status_code == unknown.status_code
    assert known.json() == unknown.json()


def test_an_owner_who_accepts_shows_up_on_the_kiosk(client, admin_auth, db_session):
    kiosk_id = _create(client, admin_auth).json()["id"]
    invitee = _user(db_session, "newshop@example.com")
    sent = {}

    class Recording(NullNotifier):
        def send_staff_invite(self, *, email, token, kiosk_name):
            sent["token"] = token

    client.app.dependency_overrides[get_notifier] = lambda: Recording()
    client.post(
        f"{KIOSKS}/{kiosk_id}/owner",
        headers=admin_auth,
        json={"email": "newshop@example.com"},
    )

    accepted = client.post(
        "/v1/app/staff/accept-invite",
        headers=_auth(invitee),
        json={"token": sent["token"]},
    )

    assert accepted.status_code == 200
    listed = client.get(f"{KIOSKS}/{kiosk_id}", headers=admin_auth).json()
    assert listed["owner_id"] == invitee.public_id
    assert listed["owner_email"] == invitee.email


# -- who may do this --------------------------------------------------------


def test_an_owner_cannot_create_a_kiosk_or_move_one(client, owner_auth, admin_auth):
    """Creating a kiosk decides what it is and therefore whose account collects
    at it. Climbing the ladder is the check that the shop is ready to sell."""
    kiosk_id = _create(client, admin_auth).json()["id"]

    assert _create(client, owner_auth, name="Mine Now").status_code == 403
    assert _stage(client, owner_auth, kiosk_id, "approved").status_code == 403
    assert (
        client.put(
            f"{KIOSKS}/{kiosk_id}/type", headers=owner_auth, json={"type": "platform"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{KIOSKS}/{kiosk_id}/owner",
            headers=owner_auth,
            json={"email": "me@example.com"},
        ).status_code
        == 403
    )


def test_creating_a_kiosk_needs_a_token(client):
    assert client.post(KIOSKS, json={"name": "Nowhere"}).status_code == 401


# -- the trail --------------------------------------------------------------


def test_the_lifecycle_is_audited(client, admin_auth, admin, db_session):
    """Who put this shop live, and when, is the first question asked when money
    turns up in the wrong account."""
    kiosk_id = _create(client, admin_auth).json()["id"]
    _stage(client, admin_auth, kiosk_id, "approved")
    client.put(f"{KIOSKS}/{kiosk_id}/type", headers=admin_auth, json={"type": "saas"})

    trail = entries_for(db_session, entity_type="kiosk", entity_id=kiosk_id)
    actions = {entry.action for entry in trail}

    assert {"kiosk.created", "kiosk.stage.changed", "kiosk.type.changed"} <= actions
    assert all(entry.actor_user_id == admin.id for entry in trail)


def test_the_type_change_records_what_it_moved_between(
    client, admin_auth, db_session
):
    kiosk_id = _create(client, admin_auth).json()["id"]
    client.put(f"{KIOSKS}/{kiosk_id}/type", headers=admin_auth, json={"type": "sold"})

    entry = next(
        e
        for e in entries_for(db_session, entity_type="kiosk", entity_id=kiosk_id)
        if e.action == "kiosk.type.changed"
    )

    assert entry.before["kiosk_type"] == KioskType.PLATFORM.value
    assert entry.after["kiosk_type"] == KioskType.SOLD.value


# ── standing a shop up in one request ───────────────────────────────────────


def test_provisioning_a_platform_kiosk_leaves_it_selling(client, admin_auth):
    response = client.post(
        "/v1/admin/kiosks/provision",
        headers=admin_auth,
        json={
            "name": "One Call Shop",
            "kiosk_type": "platform",
            "price_bw_single": "2.00",
            "price_bw_double": "3.00",
            "price_color_single": "10.00",
            "price_color_double": "20.00",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["selling"] is True
    assert body["blocked_by"] == []
    assert body["kiosk"]["onboarding_stage"] == "live"
    assert body["enrolment_code"].startswith("dve_")


def test_provisioning_a_sold_kiosk_says_what_is_missing(client, admin_auth):
    """It cannot go live -- nobody can collect at it yet -- and the response
    says so in a sentence rather than leaving an operator to infer it."""
    response = client.post(
        "/v1/admin/kiosks/provision",
        headers=admin_auth,
        json={"name": "Somebody Elses Shop", "kiosk_type": "sold"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["selling"] is False
    assert body["blocked_by"]
    assert body["kiosk"]["onboarding_stage"] != "live"


def test_provisioning_invites_the_owner_by_email(client, admin_auth):
    """Invited, not attached: somebody must consent to owning a shop."""

    class Recording(NullNotifier):
        sent: list[tuple[str, str]] = []

        def send_staff_invite(self, *, email, token, kiosk_name):
            Recording.sent.append((email, kiosk_name))

    Recording.sent = []
    client.app.dependency_overrides[get_notifier] = lambda: Recording()

    client.post(
        "/v1/admin/kiosks/provision",
        headers=admin_auth,
        json={
            "name": "Invited Shop",
            "kiosk_type": "sold",
            "owner_email": "shopkeeper@example.com",
        },
    )

    assert Recording.sent, "the invitation was never sent"
    assert Recording.sent[0] == ("shopkeeper@example.com", "Invited Shop")


def test_provisioning_is_admin_only(client, owner_auth):
    response = client.post(
        "/v1/admin/kiosks/provision",
        headers=owner_auth,
        json={"name": "Not Yours", "kiosk_type": "platform"},
    )

    assert response.status_code == 403
