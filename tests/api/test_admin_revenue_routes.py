"""What the platform took, as an admin sees it.

The number a business is run on, and the one the backend being replaced got
wrong three different ways at once -- three call sites, two ideas of which
payment states count. Here it is the same `SETTLED_PAYMENT_STATES` predicate as
an owner's own earnings page, so the two cannot disagree.

The property this router exists to protect: **there is no total.** The buckets
are four kinds of money -- ours, the owners', subscription income, and student
balances we merely hold -- and a figure adding them up would be true of nothing.
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
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk
from app.modules.payments import PaymentKind, record_wallet_payment

SECRET = "s" * 32
BOX_KEY = Fernet.generate_key().decode()
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=BOX_KEY,
    CORS_ORIGINS="https://admin.printvendo.com",
    PUBLIC_BASE_URL="https://api.printvendo.com",
)

REVENUE = "/v1/admin/revenue"


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
def admin_auth(db_session) -> dict[str, str]:
    return _auth(_user(db_session, "ops@printvendo.com", Role.ADMIN))


@pytest.fixture
def student(db_session) -> User:
    return _user(db_session, "buyer@example.com", Role.STUDENT)


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(
        name="Revenue Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
    )
    db_session.add(kiosk)
    db_session.flush()
    return kiosk


def _spend(db_session, student, kiosk, amount: str, *, at=NOW, kind=PaymentKind.PRINT_ORDER):
    return record_wallet_payment(
        db_session,
        user_id=student.id,
        kind=kind,
        amount=Decimal(amount),
        kiosk_id=kiosk.id if kind is PaymentKind.PRINT_ORDER else None,
        now=at,
    )


def test_the_buckets_come_back_separately(client, admin_auth, db_session, student, kiosk):
    _spend(db_session, student, kiosk, "40.00")
    _spend(db_session, student, kiosk, "500.00", kind=PaymentKind.WALLET_TOPUP)

    body = client.get(REVENUE, headers=admin_auth).json()

    assert body["print_platform"]["gross_inr"] == "40.00"
    assert body["wallet_topups"]["gross_inr"] == "500.00"
    assert body["print_owners"]["gross_inr"] == "0.00"


def test_there_is_no_total(client, admin_auth, db_session, student, kiosk):
    """Adding a student's unspent balance to our takings would book a liability
    as income."""
    _spend(db_session, student, kiosk, "40.00")

    body = client.get(REVENUE, headers=admin_auth).json()

    assert "total" not in body
    assert "total_inr" not in body


def test_the_window_narrows_it(client, admin_auth, db_session, student, kiosk):
    _spend(db_session, student, kiosk, "100.00", at=NOW - timedelta(days=10))
    _spend(db_session, student, kiosk, "70.00", at=NOW)

    # Passed as a parameter rather than pasted into the string: an ISO offset
    # contains a "+", which is a space once it is in a raw query.
    body = client.get(
        REVENUE,
        headers=admin_auth,
        params={"since": (NOW - timedelta(days=1)).isoformat()},
    ).json()

    assert body["print_platform"]["gross_inr"] == "70.00"
    assert body["print_platform"]["payment_count"] == 1
    assert body["since"] is not None


def test_a_window_that_is_not_a_date_is_refused(client, admin_auth):
    """422 rather than a silently unfiltered answer. An operator who asked for
    this month and got all time would read it as a very good month."""
    assert client.get(f"{REVENUE}?since=lastweek", headers=admin_auth).status_code == 422


def test_an_empty_platform_reads_as_zero(client, admin_auth):
    body = client.get(REVENUE, headers=admin_auth).json()

    assert body["print_platform"]["gross_inr"] == "0.00"
    assert body["subscriptions"]["payment_count"] == 0


def test_revenue_is_admin_only(client, db_session):
    """An owner seeing platform revenue would see every other shop's turnover
    rolled up, which is somebody else's commercial information."""
    owner_auth = _auth(_user(db_session, "shop@example.com", Role.OWNER))

    assert client.get(REVENUE, headers=owner_auth).status_code == 403
    assert client.get(REVENUE).status_code == 401
