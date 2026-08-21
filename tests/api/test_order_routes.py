"""Placing and paying for an order, over HTTP.

The service tests prove the aggregate. These prove the wiring, and one of them
is the gate for this whole phase: a student uploads, orders, pays and has print
tasks queued without anything but HTTP being involved.

The property every test here defends is the one the rewrite exists for. In the
old backend `POST /wallet/hold` took the money and a *second* request enqueued
the print, so anything failing between them left a student charged for nothing.
Here both payment paths end in one commit, and `get_db` commits once per
request -- so if the response says paid, the tasks exist.
"""

import hashlib
import hmac
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
from app.modules.identity.models import User
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskPaper
from app.modules.printing.models import Document, DocumentState, PrintTask
from app.modules.wallet.ledger import credit
from app.modules.wallet.models import EntryKind

SECRET = "s" * 32
PLATFORM_KEY_ID = "rzp_test_platform"
PLATFORM_KEY_SECRET = "platform_key_secret"

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="https://printvendo.com",
    RAZORPAY_KEY_ID=PLATFORM_KEY_ID,
    RAZORPAY_KEY_SECRET=PLATFORM_KEY_SECRET,
)


class FakeRazorpay:
    """Stands in for the HTTP client, at the protocol the module declares.

    Nothing about the signature check is faked: `verify` below signs with the
    real HMAC, exactly as Razorpay's checkout would, so the security-critical
    part of this route is exercised rather than skipped.
    """

    def __init__(self) -> None:
        self.orders: list[dict] = []

    def create_order(self, *, amount_paise, receipt, credentials) -> str:
        self.orders.append(
            {"amount_paise": amount_paise, "receipt": receipt, "key": credentials.key_id}
        )
        return f"order_TEST{len(self.orders)}"

    def refund(self, *, razorpay_payment_id, amount_paise, idempotency_key, credentials):
        return "rfnd_TEST"


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
def student(db_session) -> User:
    user = User(email="orderer@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def auth(student) -> dict[str, str]:
    token = create_token(
        student.public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5)
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    kiosk = Kiosk(
        name="Campus Print",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
        accepts_wallet=True,
        price_bw_single=Decimal("2.00"),
        price_bw_double=Decimal("1.50"),
        price_color_single=Decimal("10.00"),
        price_color_double=Decimal("8.00"),
    )
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id, capacity=500, used=0))
    db_session.flush()
    return kiosk


@pytest.fixture
def document(db_session, student) -> Document:
    document = Document(
        user_id=student.id,
        original_filename="thesis.pdf",
        page_count=10,
        original_path="originals/2026/08/t.pdf",
        state=DocumentState.READY,
    )
    db_session.add(document)
    db_session.flush()
    return document


def place(client, auth, kiosk, document, *, method="wallet"):
    return client.post(
        "/v1/app/orders",
        headers=auth,
        json={
            "kiosk_id": kiosk.public_id,
            "payment_method": method,
            "items": [{"document_id": document.public_id, "colour": False}],
        },
    )


# ── browsing ────────────────────────────────────────────────────────────────


def test_a_student_can_see_the_shops_that_can_print(client, auth, kiosk):
    """A student manages no kiosks, so their management scope is empty. If this
    route used it they would see nothing at all."""
    response = client.get("/v1/app/kiosks", headers=auth)

    assert response.status_code == 200
    listed = response.json()
    assert [k["id"] for k in listed] == [kiosk.public_id]
    assert listed[0]["price_bw_single"] == "2.00"


def test_the_kiosk_list_says_nothing_about_who_owns_a_shop(client, auth, kiosk):
    """A student has no business knowing which shops share an owner. The
    response type has no field for it, so it cannot leak by accident."""
    body = client.get("/v1/app/kiosks", headers=auth).json()[0]

    assert "owner" not in str(body).lower()
    assert set(body) == {
        "id",
        "name",
        "accepts_wallet",
        "is_out_of_paper",
        "sheets_remaining",
        "price_bw_single",
        "price_bw_double",
        "price_color_single",
        "price_color_double",
    }


# ── the gate for this phase ─────────────────────────────────────────────────


def test_a_wallet_order_is_placed_paid_and_queued_over_http(
    client, auth, kiosk, document, db_session, student
):
    """The whole point, end to end and through the HTTP layer.

    If this passes, "paid but never printed" is not reachable by the wallet
    route: the same request that reports the order paid is the one that created
    the tasks.
    """
    credit(
        db_session,
        user_id=student.id,
        amount=Decimal("100.00"),
        kind=EntryKind.TOPUP,
        reference="pay_seed",
    )

    placed = place(client, auth, kiosk, document)
    assert placed.status_code == 201, placed.text
    order = placed.json()
    assert order["state"] == "awaiting_payment"
    assert order["total_inr"] == "20.00"
    assert order["items"][0]["filename"] == "thesis.pdf"

    paid = client.post(
        f"/v1/app/orders/{order['id']}/pay/wallet", headers=auth
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["state"] == "paid"

    tasks = db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).all()
    assert len(tasks) == 1

    balance = client.get("/v1/app/wallet", headers=auth).json()
    assert balance["balance_inr"] == "80.00"


def test_a_gateway_order_is_paid_and_queued_over_http(
    client, auth, kiosk, document, db_session, razorpay
):
    """The other branch into the same commit. The signature is real HMAC, signed
    the way Razorpay's checkout signs it."""
    placed = place(client, auth, kiosk, document, method="gateway")
    assert placed.status_code == 201, placed.text
    order = placed.json()

    checkout = client.post(f"/v1/app/orders/{order['id']}/checkout", headers=auth)
    assert checkout.status_code == 200, checkout.text
    opened = checkout.json()
    assert opened["razorpay_key_id"] == PLATFORM_KEY_ID
    # The gateway is asked for exactly the total the server quoted -- not a
    # number recomputed here, and not one the client supplied. The old backend
    # accepted a client-side amount at the gateway.
    assert razorpay.orders[0]["amount_paise"] == int(Decimal(order["total_inr"]) * 100)
    assert opened["amount_inr"] == order["total_inr"]

    razorpay_order_id = opened["razorpay_order_id"]
    payment_id = "pay_LIVE1"
    signature = hmac.new(
        PLATFORM_KEY_SECRET.encode(),
        f"{razorpay_order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()

    verified = client.post(
        f"/v1/app/orders/{order['id']}/verify",
        headers=auth,
        json={
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["state"] == "paid"
    assert db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).count() == 1


def test_the_checkout_never_returns_the_key_secret(client, auth, kiosk, document):
    placed = place(client, auth, kiosk, document, method="gateway")
    body = client.post(
        f"/v1/app/orders/{placed.json()['id']}/checkout", headers=auth
    ).text

    assert PLATFORM_KEY_SECRET not in body


def test_a_forged_callback_prints_nothing(
    client, auth, kiosk, document, db_session
):
    """The attack: a browser claiming a payment succeeded. Without a valid
    signature nothing may advance towards a printer."""
    placed = place(client, auth, kiosk, document, method="gateway")
    order = placed.json()
    opened = client.post(
        f"/v1/app/orders/{order['id']}/checkout", headers=auth
    ).json()

    forged = client.post(
        f"/v1/app/orders/{order['id']}/verify",
        headers=auth,
        json={
            "razorpay_order_id": opened["razorpay_order_id"],
            "razorpay_payment_id": "pay_FAKE",
            "razorpay_signature": "ff" * 32,
        },
    )

    assert forged.status_code == 400
    assert db_session.query(PrintTask).count() == 0
    assert (
        client.get(f"/v1/app/orders/{order['id']}", headers=auth).json()["state"]
        == "awaiting_payment"
    )


# ── isolation ───────────────────────────────────────────────────────────────


def test_another_students_order_is_not_found_rather_than_forbidden(
    client, auth, kiosk, document, db_session
):
    """403 would confirm the order exists. Both answers are the same sentence."""
    placed = place(client, auth, kiosk, document)
    order_id = placed.json()["id"]

    intruder = User(email="nosy@example.com", hashed_password="x")
    db_session.add(intruder)
    db_session.flush()
    token = create_token(
        intruder.public_id, TokenType.ACCESS, SECRET, timedelta(minutes=5)
    )
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/v1/app/orders/{order_id}", headers=headers).status_code == 404
    assert (
        client.post(
            f"/v1/app/orders/{order_id}/pay/wallet", headers=headers
        ).status_code
        == 404
    )


def test_someone_elses_document_cannot_be_ordered(
    client, auth, kiosk, db_session
):
    other = User(email="author@example.com", hashed_password="x")
    db_session.add(other)
    db_session.flush()
    theirs = Document(
        user_id=other.id,
        original_filename="private.pdf",
        page_count=4,
        original_path="originals/2026/08/p.pdf",
        state=DocumentState.READY,
    )
    db_session.add(theirs)
    db_session.flush()

    response = place(client, auth, kiosk, theirs)

    # 404, and the same sentence a document that never existed gets.
    assert response.status_code == 404
    assert db_session.query(PrintTask).count() == 0


def test_an_order_cannot_be_placed_without_signing_in(client, kiosk, document):
    response = client.post(
        "/v1/app/orders",
        json={
            "kiosk_id": kiosk.public_id,
            "payment_method": "wallet",
            "items": [{"document_id": document.public_id}],
        },
    )

    assert response.status_code == 401


# ── the wallet ──────────────────────────────────────────────────────────────


def test_an_empty_wallet_neither_pays_nor_queues(
    client, auth, kiosk, document, db_session
):
    placed = place(client, auth, kiosk, document)

    response = client.post(
        f"/v1/app/orders/{placed.json()['id']}/pay/wallet", headers=auth
    )

    assert response.status_code == 400
    assert db_session.query(PrintTask).count() == 0


def test_a_top_up_credits_nothing_until_the_money_arrives(
    client, auth, db_session, student
):
    """Crediting here would hand out balance for a checkout the student can
    simply abandon."""
    response = client.post(
        "/v1/app/wallet/topup", headers=auth, json={"amount_inr": "200.00"}
    )

    assert response.status_code == 200
    assert client.get("/v1/app/wallet", headers=auth).json()["balance_inr"] == "0.00"


def test_a_silly_top_up_is_refused(client, auth):
    assert (
        client.post(
            "/v1/app/wallet/topup", headers=auth, json={"amount_inr": "1.00"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/app/wallet/topup", headers=auth, json={"amount_inr": "99999.00"}
        ).status_code
        == 400
    )


def test_the_statement_shows_what_happened(client, auth, db_session, student):
    credit(
        db_session,
        user_id=student.id,
        amount=Decimal("50.00"),
        kind=EntryKind.TOPUP,
        reference="pay_statement",
    )

    entries = client.get("/v1/app/wallet/statement", headers=auth).json()

    assert len(entries) == 1
    assert entries[0]["kind"] == "topup"
    assert entries[0]["amount_inr"] == "50.00"
    assert entries[0]["balance_after_inr"] == "50.00"
