"""Plans, negotiated terms, and the trial that lets a shop start collecting.

D13 in one router: an admin can put an owner on a plan, give that one owner a
rate nobody else gets, and grant a trial. The last of those is not a courtesy --
a subscription inside its trial is in force, and being in force is half of what
the payment gate requires before a SOLD kiosk collects into its owner's own
Razorpay. Granting a trial turns a shop's takings on.
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
from app.modules.billing import has_active_subscription
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
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

PLANS = "/v1/admin/plans"


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
def owner(db_session) -> User:
    return _user(db_session, "shopkeeper@example.com", Role.OWNER)


@pytest.fixture
def owner_auth(owner) -> dict[str, str]:
    return _auth(owner)


def _create_plan(client, auth, **overrides):
    body = {
        "name": "Standard",
        "monthly_price": "499.00",
        "max_kiosks": 2,
        "price_floor_bw": "1.00",
        "price_ceiling_bw": "5.00",
    } | overrides
    return client.post(PLANS, headers=auth, json=body)


def _owner_path(owner: User) -> str:
    return f"/v1/admin/owners/{owner.public_id}/billing"


# -- plans ------------------------------------------------------------------


def test_a_plan_can_be_created_and_listed(client, admin_auth):
    created = _create_plan(client, admin_auth)

    assert created.status_code == 201
    assert created.json()["monthly_price"] == "499.00"
    assert created.json()["id"].startswith("sub_")

    listed = client.get(PLANS, headers=admin_auth).json()
    assert [p["name"] for p in listed] == ["Standard"]


def test_a_price_is_money_not_a_float(client, admin_auth):
    """Rupees with two places, `ROUND_HALF_UP`. A price that arrives as a float
    has already lost precision before anybody can round it."""
    created = _create_plan(client, admin_auth, monthly_price="499.005")

    assert created.json()["monthly_price"] == "499.01"


def test_a_plan_with_an_upside_down_band_is_refused(client, admin_auth):
    refused = _create_plan(
        client, admin_auth, price_floor_bw="9.00", price_ceiling_bw="1.00"
    )

    assert refused.status_code == 400
    assert "floor" in refused.json()["detail"]


def test_two_plans_cannot_share_a_name(client, admin_auth):
    _create_plan(client, admin_auth)

    assert _create_plan(client, admin_auth).status_code == 409


def test_a_retired_plan_leaves_the_list_but_keeps_its_id(client, admin_auth):
    """Deleting it would orphan every subscription that names it."""
    plan_id = _create_plan(client, admin_auth).json()["id"]

    client.patch(f"{PLANS}/{plan_id}", headers=admin_auth, json={"is_active": False})

    assert client.get(PLANS, headers=admin_auth).json() == []
    assert (
        client.get(f"{PLANS}?include_retired=true", headers=admin_auth).json()[0]["id"]
        == plan_id
    )


def test_the_published_discount_ladder_is_set_per_duration(client, admin_auth):
    plan_id = _create_plan(client, admin_auth).json()["id"]

    set_ladder = client.put(
        f"{PLANS}/{plan_id}/discounts",
        headers=admin_auth,
        json={"duration_months": 12, "percent": "10.00"},
    )

    assert set_ladder.status_code == 200
    assert set_ladder.json()["discounts"] == [{"duration_months": 12, "percent": "10.00"}]


def test_a_discount_for_a_duration_nobody_can_buy_is_refused(client, admin_auth):
    plan_id = _create_plan(client, admin_auth).json()["id"]

    refused = client.put(
        f"{PLANS}/{plan_id}/discounts",
        headers=admin_auth,
        json={"duration_months": 9, "percent": "10.00"},
    )

    assert refused.status_code == 400


# -- one owner's terms ------------------------------------------------------


def test_granting_a_trial_lets_that_owner_collect(client, admin_auth, owner, db_session):
    """The money-routing half of D13. Until this, a SOLD kiosk's owner had no
    way to reach an active subscription at all, so no such kiosk could go live."""
    plan_id = _create_plan(client, admin_auth).json()["id"]

    granted = client.post(
        f"{_owner_path(owner)}/trial",
        headers=admin_auth,
        json={"plan_id": plan_id, "days": 30},
    )

    assert granted.status_code == 200
    assert granted.json()["subscription"]["on_trial"] is True
    assert granted.json()["subscription"]["total_amount"] == "0.00"
    assert has_active_subscription(db_session, owner.id) is True


def test_a_trial_can_be_ended_today(client, admin_auth, owner, db_session):
    plan_id = _create_plan(client, admin_auth).json()["id"]
    client.post(
        f"{_owner_path(owner)}/trial",
        headers=admin_auth,
        json={"plan_id": plan_id, "days": 30},
    )

    ended = client.delete(f"{_owner_path(owner)}/trial", headers=admin_auth)

    assert ended.status_code == 200
    assert ended.json()["subscription"]["on_trial"] is False
    assert has_active_subscription(db_session, owner.id) is False


def test_ending_a_trial_nobody_has_is_a_clear_404(client, admin_auth, owner):
    assert client.delete(f"{_owner_path(owner)}/trial", headers=admin_auth).status_code == 404


def test_an_owners_negotiated_rate_shows_up_in_their_quote(client, admin_auth, owner):
    """D13: one owner on a different annual rate, which the old system could not
    express at all."""
    plan_id = _create_plan(client, admin_auth).json()["id"]
    client.post(
        f"{_owner_path(owner)}/trial",
        headers=admin_auth,
        json={"plan_id": plan_id, "days": 30},
    )
    client.put(
        f"{_owner_path(owner)}/discounts",
        headers=admin_auth,
        json={"duration_months": 12, "percent": "25.00", "note": "signed three shops"},
    )
    client.put(
        f"{_owner_path(owner)}/price",
        headers=admin_auth,
        json={"monthly_price": "400.00"},
    )

    quote = client.get(
        f"{_owner_path(owner)}/quote?duration_months=12", headers=admin_auth
    ).json()

    assert quote["monthly_price"] == "400.00"
    assert quote["discount_percent"] == "25.00"
    assert quote["discount_source"] == "owner"
    assert quote["total"] == "3600.00"


def test_a_negotiated_rate_can_be_taken_away(client, admin_auth, owner):
    plan_id = _create_plan(client, admin_auth).json()["id"]
    client.post(
        f"{_owner_path(owner)}/trial",
        headers=admin_auth,
        json={"plan_id": plan_id, "days": 30},
    )
    client.put(
        f"{PLANS}/{plan_id}/discounts",
        headers=admin_auth,
        json={"duration_months": 12, "percent": "10.00"},
    )
    client.put(
        f"{_owner_path(owner)}/discounts",
        headers=admin_auth,
        json={"duration_months": 12, "percent": "25.00"},
    )

    client.delete(f"{_owner_path(owner)}/discounts/12", headers=admin_auth)

    quote = client.get(
        f"{_owner_path(owner)}/quote?duration_months=12", headers=admin_auth
    ).json()
    assert quote["discount_source"] == "plan"


def test_an_owners_billing_reads_back_what_was_granted(client, admin_auth, owner):
    plan_id = _create_plan(client, admin_auth).json()["id"]
    client.post(
        f"{_owner_path(owner)}/trial",
        headers=admin_auth,
        json={"plan_id": plan_id, "days": 30},
    )
    client.put(
        f"{_owner_path(owner)}/discounts",
        headers=admin_auth,
        json={"duration_months": 12, "percent": "25.00", "note": "three shops"},
    )

    billing = client.get(_owner_path(owner), headers=admin_auth).json()

    assert billing["owner_id"] == owner.public_id
    assert billing["subscription"]["plan_id"] == plan_id
    assert billing["subscription"]["on_trial"] is True
    assert billing["discounts"] == [
        {"duration_months": 12, "percent": "25.00", "note": "three shops"}
    ]


def test_billing_for_somebody_who_is_not_a_user_is_not_found(client, admin_auth):
    response = client.get(
        "/v1/admin/owners/usr_0000000000000000/billing", headers=admin_auth
    )

    assert response.status_code == 404


def test_an_owner_with_no_subscription_reads_as_having_none(client, admin_auth, owner):
    """Null, not a fabricated free one. "This shop cannot collect" is the fact
    the console exists to show."""
    billing = client.get(_owner_path(owner), headers=admin_auth).json()

    assert billing["subscription"] is None


# -- who may do this --------------------------------------------------------


def test_an_owner_cannot_price_their_own_subscription(client, owner_auth, owner, admin_auth):
    """The obvious one, and the reason this is a separate router rather than a
    wider scope on an owner's own billing page."""
    plan_id = _create_plan(client, admin_auth).json()["id"]

    assert _create_plan(client, owner_auth, name="Free Plan").status_code == 403
    assert (
        client.post(
            f"{_owner_path(owner)}/trial",
            headers=owner_auth,
            json={"plan_id": plan_id, "days": 3650},
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"{_owner_path(owner)}/price",
            headers=owner_auth,
            json={"monthly_price": "0.00"},
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"{_owner_path(owner)}/discounts",
            headers=owner_auth,
            json={"duration_months": 12, "percent": "100.00"},
        ).status_code
        == 403
    )


def test_the_plan_list_needs_a_token(client):
    assert client.get(PLANS).status_code == 401


# -- the trail --------------------------------------------------------------


def test_commercial_terms_are_audited_against_the_owner(
    client, admin_auth, admin, owner, db_session
):
    """A rate nobody can explain in a year is a rate somebody will argue about.
    Filed against the owner, where the rest of that account's money decisions
    already live."""
    plan_id = _create_plan(client, admin_auth).json()["id"]
    client.post(
        f"{_owner_path(owner)}/trial",
        headers=admin_auth,
        json={"plan_id": plan_id, "days": 30},
    )
    client.put(
        f"{_owner_path(owner)}/discounts",
        headers=admin_auth,
        json={"duration_months": 12, "percent": "25.00", "note": "three shops"},
    )
    client.put(
        f"{_owner_path(owner)}/price",
        headers=admin_auth,
        json={"monthly_price": "400.00"},
    )

    trail = entries_for(db_session, entity_type="user", entity_id=owner.public_id)
    actions = {entry.action for entry in trail}

    assert {
        "billing.trial.granted",
        "billing.discount.set",
        "billing.price.negotiated",
    } <= actions
    assert all(entry.actor_user_id == admin.id for entry in trail)


def test_a_trial_grant_records_how_long_it_runs(client, admin_auth, owner, db_session):
    plan_id = _create_plan(client, admin_auth).json()["id"]
    client.post(
        f"{_owner_path(owner)}/trial",
        headers=admin_auth,
        json={"plan_id": plan_id, "days": 45},
    )

    entry = next(
        e
        for e in entries_for(db_session, entity_type="user", entity_id=owner.public_id)
        if e.action == "billing.trial.granted"
    )

    assert entry.after["days"] == 45
    assert entry.after["plan_id"] == plan_id


def test_a_plan_is_audited_as_a_plan_not_as_an_owner(client, admin_auth, db_session):
    """A plan is not somebody's account. Filing it under the admin who made it
    would make "what happened to this plan" unanswerable."""
    plan_id = _create_plan(client, admin_auth).json()["id"]

    trail = entries_for(db_session, entity_type="plan", entity_id=plan_id)

    assert [e.action for e in trail] == ["billing.plan.created"]
    assert trail[0].after["monthly_price"] == "499.00"


def test_a_price_change_records_both_sides(client, admin_auth, db_session):
    plan_id = _create_plan(client, admin_auth).json()["id"]

    client.patch(
        f"{PLANS}/{plan_id}", headers=admin_auth, json={"monthly_price": "599.00"}
    )

    entry = next(
        e
        for e in entries_for(db_session, entity_type="plan", entity_id=plan_id)
        if e.action == "billing.plan.updated"
    )

    assert entry.before["monthly_price"] == "499.00"
    assert entry.after["monthly_price"] == "599.00"


def test_money_in_the_trail_is_a_string_not_a_float(client, admin_auth, db_session):
    """JSON has no decimal type. A rupee amount that round-trips through a float
    has lost precision in the one place people look to find out what it was."""
    plan_id = _create_plan(client, admin_auth, monthly_price="0.10").json()["id"]

    entry = entries_for(db_session, entity_type="plan", entity_id=plan_id)[0]

    assert entry.after["monthly_price"] == "0.10"
    assert isinstance(entry.after["monthly_price"], str)
