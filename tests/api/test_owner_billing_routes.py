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
    INVOICE_ISSUER_NAME="Printvendo",
    INVOICE_ISSUER_LINES="Printvendo Technologies|12 MG Road, Bengaluru 560001",
    INVOICE_ISSUER_EMAIL="billing@printvendo.com",
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


# ── the browser coming back ─────────────────────────────────────────────────
#
# The webhook is not the only way a capture arrives, and it must not be the only
# way one settles. An owner who has just paid is looking at a page; the webhook
# may be seconds away or, if the endpoint is misconfigured, never. A purchase
# that can only complete out of sight is a button somebody presses twice.


def _signature(order_id: str, payment_id: str, secret: str) -> str:
    import hashlib
    import hmac

    return hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()


def _bought(client, auth, plan) -> dict:
    return client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 6},
    ).json()


def test_the_browsers_callback_puts_the_subscription_in_force(
    client, auth, plan, db_session
):
    checkout = _bought(client, auth, plan)

    response = client.post(
        f"/v1/owner/billing/subscription/{checkout['order_id']}/verify",
        headers=auth,
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": "pay_sub_1",
            "razorpay_signature": _signature(
                checkout["razorpay_order_id"], "pay_sub_1", "platform_secret"
            ),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert response.json()["covered_until"] is not None


def test_a_forged_callback_changes_nothing(client, auth, plan, db_session):
    """The same rule as a print order: a callback that does not verify moves no
    money and starts no term, so a shop cannot turn its own takings on by
    posting a made-up receipt."""
    checkout = _bought(client, auth, plan)

    response = client.post(
        f"/v1/owner/billing/subscription/{checkout['order_id']}/verify",
        headers=auth,
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": "pay_sub_2",
            "razorpay_signature": "not-a-signature",
        },
    )

    assert response.status_code == 400
    assert db_session.query(Subscription).one().status is SubscriptionStatus.PENDING_PAYMENT


def test_somebody_elses_subscription_is_not_found(client, auth, plan, db_session):
    """A subscription belongs to whoever bought it. Another owner's is 404 --
    the same answer as one that never existed."""
    stranger = User(email="stranger@example.com", hashed_password="x")
    db_session.add(stranger)
    db_session.flush()
    identity_repo.grant_role(db_session, stranger.id, Role.OWNER)
    theirs = Subscription(
        user_id=stranger.id,
        plan_id=plan.id,
        duration_months=1,
        monthly_price_charged=Decimal("1000.00"),
        total_amount=Decimal("1000.00"),
        status=SubscriptionStatus.PENDING_PAYMENT,
    )
    db_session.add(theirs)
    db_session.flush()

    response = client.post(
        f"/v1/owner/billing/subscription/{theirs.public_id}/verify",
        headers=auth,
        json={
            "razorpay_order_id": "order_x",
            "razorpay_payment_id": "pay_x",
            "razorpay_signature": "x",
        },
    )

    assert response.status_code == 404
    # The sentence, not just the code: a route that does not exist answers 404
    # as well, and a test that cannot tell them apart passes before the feature
    # is written.
    assert response.json()["detail"] == "That subscription does not exist."


def test_a_receipt_for_something_else_cannot_buy_a_subscription(
    client, auth, plan, db_session, owner
):
    """A genuine, correctly signed receipt — for a different payment.

    Both ids are inside the signed string, so a signature cannot be moved
    between payments; what this pins is the step after that. The payment names
    the subscription it bought, and a receipt naming none of them must not start
    a six-month term. Without that check an owner could top up their wallet by a
    rupee and present the receipt here.
    """
    from app.modules.payments.models import Payment, PaymentKind, PaymentSource

    checkout = _bought(client, auth, plan)
    topup = Payment(
        user_id=owner.id,
        kind=PaymentKind.WALLET_TOPUP,
        source=PaymentSource.PLATFORM_GATEWAY,
        razorpay_order_id="order_topup",
        amount_inr=Decimal("1.00"),
    )
    db_session.add(topup)
    db_session.flush()

    response = client.post(
        f"/v1/owner/billing/subscription/{checkout['order_id']}/verify",
        headers=auth,
        json={
            "razorpay_order_id": "order_topup",
            "razorpay_payment_id": "pay_sub_3",
            "razorpay_signature": _signature(
                "order_topup", "pay_sub_3", "platform_secret"
            ),
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "No payment was started for that subscription."
    assert db_session.query(Subscription).one().status is SubscriptionStatus.PENDING_PAYMENT


def test_the_browser_and_the_webhook_settle_the_same_capture_once(
    client, auth, plan, db_session
):
    """Whichever arrives first wins; the second is refused by the unique payment
    id rather than buying a second term."""
    from app.modules.payments.models import Payment

    checkout = _bought(client, auth, plan)
    signature = _signature(checkout["razorpay_order_id"], "pay_sub_4", "platform_secret")
    body = {
        "razorpay_order_id": checkout["razorpay_order_id"],
        "razorpay_payment_id": "pay_sub_4",
        "razorpay_signature": signature,
    }
    client.post(
        f"/v1/owner/billing/subscription/{checkout['order_id']}/verify",
        headers=auth,
        json=body,
    )
    expires = db_session.query(Subscription).one().expires_at

    from app.api.deps import WebhookSettlement

    WebhookSettlement().settle_subscription(db_session, db_session.query(Payment).one())

    assert db_session.query(Subscription).one().expires_at == expires


# ── the paper an owner files ────────────────────────────────────────────────
#
# The legacy owner app had a printable invoice; the rewire dropped it because
# the endpoint behind it did not survive. A shop that pays for software needs a
# document saying what it paid for, and this is bytes from an authenticated
# route rather than a URL -- the same rule as the student receipt and the
# account-ownership proof, and for the same reason: the old dashboard built
# links, and a document that failed to load looked exactly like one that was
# never there.


def _pdf_text(body: bytes) -> str:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _paid_subscription(client, auth, plan, db_session):
    """Buy one and let the browser settle it, exactly as an owner would."""
    checkout = client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 6},
    ).json()
    client.post(
        f"/v1/owner/billing/subscription/{checkout['order_id']}/verify",
        headers=auth,
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": "pay_invoice_1",
            "razorpay_signature": _signature(
                checkout["razorpay_order_id"], "pay_invoice_1", "platform_secret"
            ),
        },
    )
    return checkout["order_id"]


def test_an_owner_downloads_the_invoice_for_what_they_paid(
    client, auth, plan, db_session, owner
):
    subscription_id = _paid_subscription(client, auth, plan, db_session)

    response = client.get(
        f"/v1/owner/billing/subscription/{subscription_id}/invoice", headers=auth
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")

    text = _pdf_text(response.content)
    assert "Pro" in text
    assert "6,000.00" in text
    assert owner.email in text
    # The reference that matches this document to a line on a bank statement.
    assert "pay_invoice_1" in text


def test_the_invoice_is_named_by_its_own_number(client, auth, plan, db_session):
    """A downloads folder full of `invoice.pdf` is a downloads folder with one
    invoice in it. The filename is the number printed on the document, so the
    two can be matched without opening it."""
    subscription_id = _paid_subscription(client, auth, plan, db_session)

    response = client.get(
        f"/v1/owner/billing/subscription/{subscription_id}/invoice", headers=auth
    )

    assert subscription_id.upper() in response.headers["content-disposition"]


def test_a_subscription_nobody_has_paid_for_has_no_invoice_yet(
    client, auth, plan, db_session
):
    """It is a quote until the money arrives. A document headed TOTAL PAID
    against a pending purchase is one somebody can wave at a shop."""
    checkout = client.post(
        "/v1/owner/billing/subscription",
        headers=auth,
        json={"plan_id": plan.public_id, "duration_months": 6},
    ).json()

    response = client.get(
        f"/v1/owner/billing/subscription/{checkout['order_id']}/invoice", headers=auth
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "That subscription has not been paid for, so there is no invoice for it yet."
    )


def test_another_owners_invoice_is_not_found(client, auth, plan, db_session):
    """An invoice carries a name, an email and what somebody pays. Somebody
    else's is 404 -- the same answer as one that never existed."""
    stranger = User(email="stranger-invoice@example.com", hashed_password="x")
    db_session.add(stranger)
    db_session.flush()
    identity_repo.grant_role(db_session, stranger.id, Role.OWNER)
    theirs = Subscription(
        user_id=stranger.id,
        plan_id=plan.id,
        duration_months=1,
        monthly_price_charged=Decimal("1000.00"),
        total_amount=Decimal("1000.00"),
        status=SubscriptionStatus.ACTIVE,
    )
    db_session.add(theirs)
    db_session.flush()

    response = client.get(
        f"/v1/owner/billing/subscription/{theirs.public_id}/invoice", headers=auth
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "That subscription does not exist."


def test_the_invoice_carries_the_issuer_from_configuration(
    client, auth, plan, db_session
):
    """Whose name is at the top is a legal detail that changes without the
    software changing, so it is configuration rather than a constant."""
    subscription_id = _paid_subscription(client, auth, plan, db_session)

    text = _pdf_text(
        client.get(
            f"/v1/owner/billing/subscription/{subscription_id}/invoice", headers=auth
        ).content
    )

    assert "Printvendo Technologies" in text
    assert "Bengaluru 560001" in text


def test_the_owners_past_subscriptions_are_listed_so_they_can_be_invoiced(
    client, auth, plan, db_session
):
    """`/billing` returned only what is in force, which is the right answer to
    "am I covered" and no answer at all to "where is last year's invoice"."""
    _paid_subscription(client, auth, plan, db_session)

    body = client.get("/v1/owner/billing", headers=auth).json()

    assert [row["id"] for row in body["history"]] == [body["subscription"]["id"]]
