"""Everything about one order, for the person who has to answer for it.

An operator handling "I was charged twice and nothing came out" needs the whole
row: who paid, from which account it was collected, what Razorpay called it,
and what has already been given back. None of that is on the owner surface, and
deliberately so -- a shop identifies a document, never a person.

**This is a second response type, not a loosened one.** `OwnerOrderResponse`
still has no field for a student and must not grow one: that absence is what
makes the owner routes incapable of leaking identity however they are later
edited. Admin gets a wider view by having its own type, which is the same shape
this codebase uses everywhere else admin sees more.
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
from app.modules.kiosks.enums import AssignmentRole, KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskAssignment, KioskPaper
from app.modules.orders.models import PaymentMethod
from app.modules.orders.service import RequestedDocument, pay_with_wallet, place_order
from app.modules.printing import PrintOptions
from app.modules.printing.models import Document, DocumentState
from app.modules.wallet import EntryKind, credit

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

STUDENT_EMAIL = "ravi.kumar@university.edu"


class FakeRazorpay:
    def create_order(self, *, amount_paise, receipt, credentials) -> str:
        return f"order_{receipt}"

    def refund(self, *, razorpay_payment_id, amount_paise, **kwargs) -> str:
        return "rfnd_1"


@pytest.fixture
def client(db_session) -> TestClient:
    app = create_app(SETTINGS)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_secret] = lambda: SECRET
    app.dependency_overrides[get_notifier] = lambda: NullNotifier()
    app.dependency_overrides[get_razorpay] = lambda: FakeRazorpay()
    return TestClient(app, raise_server_exceptions=False)


def _user(db_session, email: str, *roles: Role, name: str | None = None) -> User:
    user = User(email=email, hashed_password="x", full_name=name)
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
def an_admin(db_session) -> User:
    return _user(db_session, "ops@printvendo.com", Role.ADMIN)


@pytest.fixture
def owner(db_session) -> User:
    return _user(db_session, "shop@example.com", Role.OWNER)


@pytest.fixture
def student(db_session) -> User:
    person = _user(db_session, STUDENT_EMAIL, Role.STUDENT, name="Ravi Kumar")
    credit(
        db_session,
        user_id=person.id,
        amount=Decimal("500.00"),
        kind=EntryKind.TOPUP,
        reference="admin_order_seed",
    )
    return person


@pytest.fixture
def kiosk(db_session, owner) -> Kiosk:
    kiosk = Kiosk(
        name="Campus Print",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
        accepts_wallet=True,
        price_bw_single=Decimal("2.00"),
        price_bw_double=Decimal("2.00"),
        price_color_single=Decimal("10.00"),
        price_color_double=Decimal("10.00"),
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
def paid_order(db_session, student, kiosk):
    document = Document(
        user_id=student.id,
        original_filename="Medical Results.pdf",
        page_count=10,
        original_path="originals/2026/08/m.pdf",
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


def _get(client, auth, order):
    return client.get(f"/v1/admin/orders/{order.public_id}", headers=auth)


# ── what an operator can see ────────────────────────────────────────────────


def test_an_admin_sees_who_placed_the_order(client, an_admin, paid_order, student):
    """The thing the owner surface deliberately withholds. An operator handling
    a complaint has to be able to answer "which account is this?"."""
    body = _get(client, _auth(an_admin), paid_order).json()

    assert body["student"]["id"] == student.public_id
    assert body["student"]["email"] == STUDENT_EMAIL
    assert body["student"]["full_name"] == "Ravi Kumar"


def test_an_admin_sees_the_shop_and_what_was_printed(client, an_admin, paid_order):
    body = _get(client, _auth(an_admin), paid_order).json()

    assert body["kiosk_name"] == "Campus Print"
    assert body["items"][0]["filename"] == "Medical Results.pdf"
    # Ten pages, one side each: a sheet is a side unless duplex is asked for.
    assert body["items"][0]["sheets"] == 10
    assert body["total_inr"] == "20.00"


def test_an_admin_sees_how_the_money_moved(client, an_admin, paid_order):
    """Whose account collected, and what the gateway called it. This is the
    answer to "where did this rupee actually go", which is read off the payment
    and never re-derived from the kiosk."""
    body = _get(client, _auth(an_admin), paid_order).json()

    payment = body["payment"]
    assert payment["source"] == "wallet"
    assert payment["status"] == "captured"
    assert payment["amount_inr"] == "20.00"
    assert payment["refunded_inr"] == "0.00"
    # A wallet payment never touched a gateway, so there is nothing to show.
    assert payment["razorpay_payment_id"] is None
    # Nobody else collected it, which is what makes a balance refund legal.
    assert payment["collected_by"] is None


def test_the_refunds_already_given_are_listed(client, an_admin, paid_order):
    """"Have I already refunded this?" is the question an operator asks before
    refunding it again, and the order row alone cannot answer it for a partial."""
    client.post(
        f"/v1/admin/orders/{paid_order.public_id}/refund",
        headers=_auth(an_admin),
        json={"idempotency_key": "admin-detail-0001", "amount_inr": "5.00"},
    )

    body = _get(client, _auth(an_admin), paid_order).json()

    assert [r["amount_inr"] for r in body["refunds"]] == ["5.00"]
    assert body["refunds"][0]["destination"] == "wallet"
    assert body["payment"]["refunded_inr"] == "5.00"


def test_an_order_nobody_paid_for_has_no_payment_rather_than_failing(
    client, an_admin, db_session, student, kiosk
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
            RequestedDocument(document=document, options=PrintOptions.create(total_pages=2))
        ],
        method=PaymentMethod.WALLET,
    )

    body = _get(client, _auth(an_admin), order).json()

    assert body["payment"] is None
    assert body["refunds"] == []
    assert body["student"]["email"] == STUDENT_EMAIL


# ── who may see it ─────────────────────────────────────────────────────────


def test_an_owner_cannot_read_it_even_at_their_own_shop(
    client, owner, an_admin, paid_order
):
    """The whole point of the separation. An owner reading this would get the
    student identity their own routes are built to withhold, at a shop they do
    hold -- so scope is not the control here; the audience is."""
    assert _get(client, _auth(an_admin), paid_order).status_code == 200

    assert _get(client, _auth(owner), paid_order).status_code == 403


def test_a_student_cannot_read_it_for_their_own_order(client, student, paid_order):
    response = _get(client, _auth(student), paid_order)

    assert response.status_code == 403


def test_an_order_that_does_not_exist_is_a_404(client, an_admin):
    response = client.get("/v1/admin/orders/ord_doesnotexist", headers=_auth(an_admin))

    assert response.status_code == 404
    assert response.json()["detail"] == "That order does not exist."


def test_the_owner_surface_still_carries_no_student(client, owner, paid_order, kiosk):
    """The mechanism this route must not weaken. Asserted here as well as in the
    owner tests, because the temptation to add a name to `OwnerOrderResponse`
    arrives at exactly the moment somebody builds an admin view that has one.
    """
    body = client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/orders", headers=_auth(owner)
    ).text

    assert STUDENT_EMAIL not in body
    assert "Ravi Kumar" not in body
    assert "Medical Results" not in body
