"""Giving money back.

`payments.refunds` has been built and mutation-tested since the payments module
landed, and nothing exposed it over HTTP -- so the first student charged for a
print that jammed could not be refunded at all. This is that escape hatch.

Refunding is done **against an order**, not against a payment id. A complaint
arrives as "my print did not come out", which an operator can find; nobody has a
payment id to hand. The order names its payment, and the payment -- never the
kiosk, never the order -- says whose money it was and where it may go back to.
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
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskPaper
from app.modules.orders.models import PaymentMethod
from app.modules.orders.service import RequestedDocument, pay_with_wallet, place_order
from app.modules.printing import PrintOptions
from app.modules.printing.models import Document, DocumentState
from app.modules.wallet import EntryKind, balance_of, credit

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
    def __init__(self) -> None:
        self.refunds: list[tuple[str, int]] = []

    def create_order(self, *, amount_paise: int, receipt: str, credentials) -> str:
        return "order_x"

    def refund(self, *, payment_id: str, amount_paise: int, credentials, **kwargs) -> str:
        self.refunds.append((payment_id, amount_paise))
        return f"rfnd_{len(self.refunds)}"


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
def student(db_session) -> User:
    person = _user(db_session, "student@example.com", Role.STUDENT)
    credit(
        db_session,
        user_id=person.id,
        amount=Decimal("500.00"),
        kind=EntryKind.TOPUP,
        reference="refund_seed",
    )
    return person


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(
        name="Refund Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
        accepts_wallet=True,
        price_bw_single=Decimal("2.00"),
        price_bw_double=Decimal("3.00"),
        price_color_single=Decimal("10.00"),
        price_color_double=Decimal("20.00"),
    )
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id, capacity=500, used=0))
    db_session.flush()
    return kiosk


@pytest.fixture
def paid_order(db_session, student, kiosk):
    document = Document(
        user_id=student.id,
        original_filename="jammed.pdf",
        page_count=10,
        original_path="originals/2026/08/j.pdf",
        state=DocumentState.READY,
    )
    db_session.add(document)
    db_session.flush()

    order = place_order(
        db_session,
        user=student,
        kiosk=kiosk,
        requests=[
            RequestedDocument(
                document=document, options=PrintOptions.create(total_pages=10)
            )
        ],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)
    return order


def _refund(client, auth, order, **body):
    return client.post(
        f"/v1/admin/orders/{order.public_id}/refund",
        headers=auth,
        json={"idempotency_key": "refund-key-0001", **body},
    )


# ── giving it back ──────────────────────────────────────────────────────────


def test_a_wallet_payment_goes_back_to_the_wallet(
    client, admin_auth, db_session, student, paid_order
):
    """There is no gateway payment to reverse, so the balance is the only place
    it can go -- and the refund service refuses any other destination."""
    before = balance_of(db_session, user_id=student.id)

    response = _refund(client, admin_auth, paid_order, reason="Printer jammed")

    assert response.status_code == 201, response.text
    assert balance_of(db_session, user_id=student.id) == before + Decimal("20.00")


def test_the_default_is_everything_still_owed(client, admin_auth, paid_order):
    """The common case by a wide margin: the print did not come out, so all of
    it goes back. An operator should not have to retype the total."""
    body = _refund(client, admin_auth, paid_order).json()

    assert body["amount_inr"] == "20.00"


def test_a_partial_refund_is_possible(client, admin_auth, paid_order):
    """Three of five documents printed, so three fifths of it is owed back."""
    body = _refund(client, admin_auth, paid_order, amount_inr="8.00").json()

    assert body["amount_inr"] == "8.00"


def test_the_same_key_twice_returns_the_same_refund(
    client, admin_auth, db_session, student, paid_order
):
    """The rule the refund service exists to enforce, over HTTP: a retried
    request must not move money twice."""
    first = _refund(client, admin_auth, paid_order).json()
    before = balance_of(db_session, user_id=student.id)

    second = _refund(client, admin_auth, paid_order).json()

    assert second["id"] == first["id"]
    assert balance_of(db_session, user_id=student.id) == before


def test_refunding_more_than_was_paid_is_refused(client, admin_auth, paid_order):
    response = _refund(client, admin_auth, paid_order, amount_inr="100.00")

    assert response.status_code == 409


def test_an_unpaid_order_has_nothing_to_refund(
    client, admin_auth, db_session, student, kiosk
):
    document = Document(
        user_id=student.id,
        original_filename="unpaid.pdf",
        page_count=2,
        original_path="originals/2026/08/u.pdf",
        state=DocumentState.READY,
    )
    db_session.add(document)
    db_session.flush()
    order = place_order(
        db_session,
        user=student,
        kiosk=kiosk,
        requests=[
            RequestedDocument(
                document=document, options=PrintOptions.create(total_pages=2)
            )
        ],
        method=PaymentMethod.GATEWAY,
    )

    response = _refund(client, admin_auth, order)

    assert response.status_code == 404


def test_an_order_that_does_not_exist_is_a_404(client, admin_auth):
    response = client.post(
        "/v1/admin/orders/ord_aaaaaaaaaaaaaaaa/refund",
        headers=admin_auth,
        json={"idempotency_key": "refund-key-0002"},
    )

    assert response.status_code == 404


def test_refunding_is_admin_only(client, db_session, paid_order):
    """A student must not be able to refund their own order, and an owner
    refunding through the admin surface is not a thing either."""
    student_auth = _auth(_user(db_session, "nosy@example.com", Role.STUDENT))

    response = _refund(client, student_auth, paid_order)

    assert response.status_code == 403


def test_the_refund_is_recorded_against_whoever_issued_it(
    client, admin_auth, admin, db_session, paid_order
):
    """Money going back is exactly the kind of thing somebody has to answer
    for later."""
    from app.modules.ops import entries_for

    _refund(client, admin_auth, paid_order, reason="Printer jammed")

    trail = entries_for(db_session, action="payment.refunded")
    assert trail, "the refund left no trail"
    assert trail[0].actor_user_id == admin.id
