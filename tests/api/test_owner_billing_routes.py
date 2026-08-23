"""Buying a subscription, over HTTP.

The route that makes the owner and SaaS models work: without it a trial lapsing
took a shop offline with nothing the owner could do.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_razorpay, get_secret
from app.core.config import Settings
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.billing.models import Plan, Subscription, SubscriptionStatus
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    RAZORPAY_KEY_ID="rzp_test_platform",
    RAZORPAY_KEY_SECRET="platform_secret",
    CORS_ORIGINS="http://localhost:3000",
)


class FakeRazorpay:
    """Records what it was asked to collect, and whose keys were used."""

    def __init__(self) -> None:
        self.orders: list[tuple[int, str]] = []

    def create_order(self, *, amount_paise: int, receipt: str, credentials) -> str:
        self.orders.append((amount_paise, credentials.key_id))
        return f"order_{len(self.orders)}"


@pytest.fixture
def razorpay() -> FakeRazorpay:
    return FakeRazorpay()


@pytest.fixture
def client(db_session, razorpay) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    app.dependency_overrides[get_razorpay] = lambda: razorpay
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def owner(db_session) -> User:
    user = User(email="shop@example.com", hashed_password="x")
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
def plan(db_session) -> Plan:
    p = Plan(name="Pro", monthly_price=Decimal("1000.00"))
    db_session.add(p)
    db_session.flush()
    return p


def test_an_owner_with_nothing_sees_no_subscription_and_the_plans(client, auth, plan):
    body = client.get("/v1/owner/billing", headers=auth).json()

    assert body["subscription"] is None
    assert [p["name"] for p in body["plans"]] == ["Pro"]


def test_a_quote_shows_its_working(client, auth, plan):
    body = client.get(
        "/v1/owner/billing/quote",
        headers=auth,
        params={"plan_id": plan.public_id, "duration_months": 6},
    ).json()

    assert body["monthly_price"] == "1000.00"
    assert body["total"] == "6000.00"


def test_quoting_does_not_open_a_payment(client, auth, plan, db_session, razorpay):
    """An owner comparing six months against twelve must not leave a trail of
    abandoned checkouts behind them."""
    client.get(
        "/v1/owner/billing/quote",
        headers=auth,
        params={"plan_id": plan.public_id, "duration_months": 6},
    )

    assert razorpay.orders == []
    assert db_session.query(Subscription).count() == 0


def test_buying_opens_a_checkout_for_the_quoted_amount(client, auth, plan, razorpay):
    response = client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 6},
    )

    assert response.status_code == 201, response.text
    assert response.json()["amount_inr"] == "6000.00"
    assert razorpay.orders == [(600000, "rzp_test_platform")]


def test_the_platform_collects_a_subscription(client, auth, plan, razorpay):
    """Never the owner's own keys: this is our income, and routing it through
    the account they collect print takings into would have a shop pay itself."""
    client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 1},
    )

    _, key_used = razorpay.orders[0]
    assert key_used == SETTINGS.RAZORPAY_KEY_ID


def test_nothing_is_in_force_until_the_money_arrives(client, auth, plan, db_session):
    client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 6},
    )

    subscription = db_session.query(Subscription).one()
    assert subscription.status is SubscriptionStatus.PENDING_PAYMENT
    assert client.get("/v1/owner/billing", headers=auth).json()["subscription"] is None


def test_the_payment_knows_which_subscription_it_bought(client, auth, plan, db_session):
    """The link the settlement reads. Without it a captured payment has nothing
    to activate, which is what the old logged error was about."""
    from app.modules.payments.models import Payment

    client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 1},
    )

    payment = db_session.query(Payment).one()
    subscription = db_session.query(Subscription).one()
    assert payment.subscription_id == subscription.id


def test_a_plan_that_does_not_exist_is_a_404(client, auth):
    response = client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": "sub_nosuchplan12345", "duration_months": 6},
    )

    assert response.status_code == 404


def test_a_second_open_purchase_is_refused(client, auth, plan):
    client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 6},
    )

    again = client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 6},
    )

    assert again.status_code == 409


# ── the money arriving ──────────────────────────────────────────────────────


def test_a_captured_payment_puts_the_subscription_in_force(
    client, auth, plan, db_session, owner
):
    """The whole loop: buy, Razorpay captures, the gate opens.

    Settlement is what a captured payment *means* -- for a subscription, that it
    starts counting. Until this route existed the settlement logged an error
    against real money because nothing could open the checkout it was written
    for.
    """
    from app.api.deps import WebhookSettlement
    from app.modules.payments.models import Payment, PaymentStatus

    client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 6},
    )
    payment = db_session.query(Payment).one()
    payment.status = PaymentStatus.CAPTURED
    db_session.flush()

    WebhookSettlement().settle_subscription(db_session, payment)

    body = client.get("/v1/owner/billing", headers=auth).json()
    assert body["subscription"]["status"] == "active"
    assert body["subscription"]["covered_until"] is not None


def test_the_owner_can_now_collect(client, auth, plan, db_session, owner):
    """`kiosk_payment_gate` asks exactly this, and a lapsed trial used to make
    the answer false with nothing an owner could do about it."""
    from app.api.deps import WebhookSettlement
    from app.modules.billing import has_active_subscription
    from app.modules.payments.models import Payment

    client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 1},
    )
    assert has_active_subscription(db_session, owner.id) is False

    WebhookSettlement().settle_subscription(db_session, db_session.query(Payment).one())

    assert has_active_subscription(db_session, owner.id) is True


def test_settling_the_same_payment_twice_does_not_extend_it_twice(
    client, auth, plan, db_session
):
    """The webhook and the browser's callback both settle the same capture."""
    from app.api.deps import WebhookSettlement
    from app.modules.payments.models import Payment

    client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 1},
    )
    payment = db_session.query(Payment).one()

    WebhookSettlement().settle_subscription(db_session, payment)
    first = db_session.query(Subscription).one().expires_at
    WebhookSettlement().settle_subscription(db_session, payment)

    assert db_session.query(Subscription).one().expires_at == first


def test_a_payment_with_nothing_attached_is_logged_rather_than_raised(
    db_session, owner, caplog
):
    """Razorpay retries a delivery that errors. A payment we cannot attribute is
    a question for a person, not a reason to make it come back for ever."""
    import logging

    from app.api.deps import WebhookSettlement
    from app.modules.payments.models import Payment, PaymentSource, PaymentStatus
    from app.modules.payments.models import PaymentKind as Kind

    orphan = Payment(
        user_id=owner.id,
        kind=Kind.SUBSCRIPTION,
        source=PaymentSource.PLATFORM_GATEWAY,
        razorpay_order_id="order_orphan",
        amount_inr=Decimal("100.00"),
        status=PaymentStatus.CAPTURED,
    )
    db_session.add(orphan)
    db_session.flush()

    with caplog.at_level(logging.ERROR):
        WebhookSettlement().settle_subscription(db_session, orphan)

    assert "nothing to activate" in caplog.text
