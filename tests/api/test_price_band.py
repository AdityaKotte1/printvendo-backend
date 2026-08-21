"""An owner cannot charge outside the band their plan allows.

`PlatformBand` returned an unbounded band as a stand-in until billing existed,
and it passed every test in the suite for the simple reason that it never
refused anything. A guardrail that has never been seen to refuse is not known to
work, so these tests drive a real plan through a real route.

The band is a commercial term of the subscription, not a property of the
machine: the same printer on a different plan may charge differently. That is
why it is read from the owner's plan and why the join between "who owns this
kiosk" and "what is that owner paying for" happens at the composition root.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret
from app.core.config import Settings
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.billing.models import Plan, Subscription, SubscriptionStatus
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
    CORS_ORIGINS="https://owner.printvendo.com",
)


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def owner(db_session) -> User:
    user = User(email="banded@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    identity_repo.grant_role(db_session, user.id, Role.OWNER)
    db_session.flush()
    return user


@pytest.fixture
def auth(owner) -> dict[str, str]:
    token = create_token(owner.public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def kiosk(db_session, owner):
    kiosk = create_kiosk(db_session, name="Banded Shop")
    db_session.flush()
    db_session.add(
        KioskAssignment(kiosk_id=kiosk.id, user_id=owner.id, role=AssignmentRole.OWNER)
    )
    db_session.flush()
    return kiosk


def subscribe(db_session, owner, *, floor_bw, ceiling_bw):
    """Put this owner on a plan with a real band."""
    plan = Plan(
        name=f"Banded {floor_bw}-{ceiling_bw}",
        monthly_price=Decimal("499.00"),
        price_floor_bw=Decimal(floor_bw),
        price_ceiling_bw=Decimal(ceiling_bw),
        price_floor_color=Decimal("5.00"),
        price_ceiling_color=Decimal("20.00"),
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        Subscription(
            user_id=owner.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            duration_months=1,
            monthly_price_charged=Decimal("499.00"),
            total_amount=Decimal("499.00"),
            starts_at=datetime.now(UTC) - timedelta(days=1),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    db_session.flush()
    return plan


def set_price(client, auth, kiosk, bw: str):
    """Set both black-and-white prices to the same value.

    Setting only the single-sided price would trip a different rule entirely --
    a double-sided sheet may not cost less than a single-sided one -- and the
    test would then be green for a reason that has nothing to do with the band.
    Equal prices satisfy that relationship (double must be between 1x and 2x
    single), leaving the band as the only thing under test.
    """
    return client.put(
        f"/v1/owner/kiosks/{kiosk.public_id}/pricing",
        headers=auth,
        json={"bw_single": bw, "bw_double": bw},
    )


def test_a_price_above_the_ceiling_is_refused(client, auth, kiosk, owner, db_session):
    """The test the placeholder could never fail.

    The refusal must be about the *band*, not about a malformed request -- an
    earlier draft of this file used the wrong field name and passed for that
    reason, proving nothing. Asserting on the sentence keeps it honest.
    """
    subscribe(db_session, owner, floor_bw="1.00", ceiling_bw="5.00")

    response = set_price(client, auth, kiosk, "50.00")

    assert response.status_code == 400
    assert "5.00" in response.json()["detail"]
    assert db_session.get(type(kiosk), kiosk.id).price_bw_single != Decimal("50.00")


def test_a_price_below_the_floor_is_refused(client, auth, kiosk, owner, db_session):
    """A floor is not decoration: undercutting to zero at a kiosk the platform
    takes a fee on is a way to make the fee meaningless."""
    subscribe(db_session, owner, floor_bw="1.00", ceiling_bw="5.00")

    response = set_price(client, auth, kiosk, "0.10")
    assert response.status_code == 400
    assert "1.00" in response.json()["detail"]


def test_a_price_inside_the_band_is_accepted(client, auth, kiosk, owner, db_session):
    subscribe(db_session, owner, floor_bw="1.00", ceiling_bw="5.00")

    response = set_price(client, auth, kiosk, "3.50")

    assert response.status_code == 200, response.text
    assert response.json()["prices"]["bw_single"] == "3.50"


def test_the_band_follows_the_plan_not_the_kiosk(client, auth, kiosk, owner, db_session):
    """Same printer, different plan, different ceiling. That is why the band is
    read from the subscription rather than stored on the machine."""
    subscribe(db_session, owner, floor_bw="1.00", ceiling_bw="5.00")
    assert set_price(client, auth, kiosk, "9.00").status_code == 400

    # Move them to a more generous plan.
    db_session.query(Subscription).filter_by(user_id=owner.id).update(
        {"status": SubscriptionStatus.CANCELLED}
    )
    db_session.flush()
    subscribe(db_session, owner, floor_bw="1.00", ceiling_bw="20.00")

    assert set_price(client, auth, kiosk, "9.00").status_code == 200


def test_an_owner_with_no_plan_is_unbounded(client, auth, kiosk, db_session):
    """A PLATFORM kiosk has no subscribing owner and its prices are the
    platform's to set. Failing closed here would make every platform kiosk
    unpriceable -- and a SOLD or SAAS kiosk cannot reach LIVE without an active
    subscription anyway, so this case is unreachable for the kiosks a band
    exists to constrain."""
    assert set_price(client, auth, kiosk, "99.00").status_code == 200
