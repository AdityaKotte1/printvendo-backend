"""What an owner sees about their shops, and what they must never see.

Two properties. Scope isolation, which the kiosk routes already prove for reads
and which must hold identically for money. And student privacy: a shop owner has
a legitimate interest in what was printed and what it cost, and none at all in
who printed it.
"""

from datetime import timedelta
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
from app.modules.kiosks.enums import AssignmentRole, KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskAssignment, KioskPaper
from app.modules.orders.models import PaymentMethod
from app.modules.orders.service import RequestedDocument, pay_with_wallet, place_order
from app.modules.printing import PrintOptions
from app.modules.printing.models import Document, DocumentState
from app.modules.wallet.ledger import credit
from app.modules.wallet.models import EntryKind

SECRET = "s" * 32

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    CORS_ORIGINS="https://owner.printvendo.com",
)

STUDENT_EMAIL = "very.identifiable.person@university.edu"


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


def _kiosk(db_session, owner: User, name: str) -> Kiosk:
    kiosk = Kiosk(
        name=name,
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
def alice(db_session) -> User:
    return _user(db_session, "alice@example.com", Role.OWNER)


@pytest.fixture
def bob(db_session) -> User:
    return _user(db_session, "bob@example.com", Role.OWNER)


@pytest.fixture
def student(db_session) -> User:
    student = _user(db_session, STUDENT_EMAIL)
    credit(
        db_session,
        user_id=student.id,
        amount=Decimal("500.00"),
        kind=EntryKind.TOPUP,
        reference="pay_seed_owner",
    )
    return student


def a_paid_order(db_session, student: User, kiosk: Kiosk, *, pages: int = 10):
    document = Document(
        user_id=student.id,
        original_filename="Medical Results Ravi Kumar.pdf",
        page_count=pages,
        original_path=f"originals/2026/08/{pages}.pdf",
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
                document=document, options=PrintOptions.create(total_pages=pages)
            )
        ],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)
    return order


# ── privacy ─────────────────────────────────────────────────────────────────


def test_an_owner_sees_no_trace_of_who_printed(client, db_session, alice, student):
    """Not stripped -- absent. `OwnerOrderResponse` has no field for a person,
    so this cannot regress by someone adding one line to a handler."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    a_paid_order(db_session, student, kiosk)

    body = client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/orders", headers=_auth(alice)
    ).text

    assert STUDENT_EMAIL not in body
    assert student.public_id not in body
    # A document title is often the most identifying thing about a job.
    assert "Ravi Kumar" not in body


def test_the_order_response_carries_only_what_a_shop_needs(
    client, db_session, alice, student
):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    a_paid_order(db_session, student, kiosk)

    row = client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/orders", headers=_auth(alice)
    ).json()[0]

    assert set(row) == {
        "id",
        "state",
        "payment_method",
        "total_inr",
        "sheets",
        "document_count",
        "paid_at",
        "refunded_at",
        "created_at",
    }
    assert row["state"] == "paid"
    assert row["sheets"] == 10


# ── scope ───────────────────────────────────────────────────────────────────


def test_earnings_cover_only_this_owners_kiosks(client, db_session, alice, bob, student):
    alice_kiosk = _kiosk(db_session, alice, "Alice Print")
    bob_kiosk = _kiosk(db_session, bob, "Bob Print")
    a_paid_order(db_session, student, alice_kiosk, pages=10)
    a_paid_order(db_session, student, bob_kiosk, pages=50)

    mine = client.get("/v1/owner/earnings", headers=_auth(alice)).json()

    assert mine["gross_inr"] == "20.00"
    assert mine["order_count"] == 1


def test_an_owner_with_no_kiosks_earns_nothing_rather_than_everything(
    client, db_session, alice, bob, student
):
    """The accident this guards: an empty scope becoming an unfiltered query."""
    a_paid_order(db_session, student, _kiosk(db_session, alice, "Alice Print"))

    theirs = client.get("/v1/owner/earnings", headers=_auth(bob)).json()

    assert theirs["gross_inr"] == "0.00"
    assert theirs["order_count"] == 0


def test_another_owners_kiosk_orders_are_not_found_rather_than_forbidden(
    client, db_session, alice, bob, student
):
    """403 would confirm the kiosk exists, telling one shop owner something true
    about a competitor's estate."""
    alice_kiosk = _kiosk(db_session, alice, "Alice Print")
    a_paid_order(db_session, student, alice_kiosk)

    response = client.get(
        f"/v1/owner/kiosks/{alice_kiosk.public_id}/orders", headers=_auth(bob)
    )

    assert response.status_code == 404


def test_the_per_kiosk_split_agrees_with_the_total(client, db_session, alice, student):
    """Two endpoints, one set of numbers. They must not be able to disagree --
    that is exactly the legacy defect this predicate exists to prevent."""
    first = _kiosk(db_session, alice, "Shop One")
    second = _kiosk(db_session, alice, "Shop Two")
    a_paid_order(db_session, student, first, pages=10)
    a_paid_order(db_session, student, second, pages=20)

    total = client.get("/v1/owner/earnings", headers=_auth(alice)).json()
    split = client.get("/v1/owner/earnings/by-kiosk", headers=_auth(alice)).json()

    assert sum(Decimal(k["earnings"]["gross_inr"]) for k in split) == Decimal(
        total["gross_inr"]
    )
    assert len(split) == 2


def test_a_quiet_kiosk_reports_zero_rather_than_being_absent(
    client, db_session, alice, student
):
    _kiosk(db_session, alice, "Busy Shop")
    quiet = _kiosk(db_session, alice, "Quiet Shop")
    a_paid_order(db_session, student, _kiosk(db_session, alice, "Third Shop"))

    split = client.get("/v1/owner/earnings/by-kiosk", headers=_auth(alice)).json()
    by_id = {k["kiosk_id"]: k for k in split}

    assert by_id[quiet.public_id]["earnings"]["gross_inr"] == "0.00"
    assert by_id[quiet.public_id]["earnings"]["order_count"] == 0


# ── who may ask ─────────────────────────────────────────────────────────────


def test_a_student_cannot_read_a_shops_earnings(client, db_session, alice, student):
    _kiosk(db_session, alice, "Alice Print")

    assert client.get("/v1/owner/earnings", headers=_auth(student)).status_code == 403


def test_signing_in_is_required(client):
    assert client.get("/v1/owner/earnings").status_code == 401
