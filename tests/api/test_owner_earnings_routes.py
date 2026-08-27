"""What an owner sees about their shops, and what they must never see.

Two properties. Scope isolation, which the kiosk routes already prove for reads
and which must hold identically for money. And student privacy: a shop owner has
a legitimate interest in what was printed and what it cost, and none at all in
who printed it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_secret
from app.api.owner import earnings as earnings_routes
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


# ── the CSV export ──────────────────────────────────────────────────────────


def _export(client, user, kiosk, **params):
    return client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/orders/export",
        headers=_auth(user),
        params=params,
    )


def test_the_export_is_a_csv_with_a_header_and_a_row_per_order(
    client, db_session, alice, student
):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_paid_order(db_session, student, kiosk)

    response = _export(client, alice, kiosk)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = [line for line in response.text.splitlines() if line]
    assert rows[0].startswith("order_id,")
    assert len(rows) == 2
    assert order.public_id in rows[1]


def test_the_export_carries_no_trace_of_who_printed(client, db_session, alice, student):
    """The same rule as the JSON list, which has no field for a person at all.

    A CSV is built by hand rather than from a response type, so this is the one
    place that rule has to be kept by a test instead of by a schema. Filenames
    are left out for the same reason: "Medical Results Ravi Kumar.pdf" names a
    person as surely as an email address does.
    """
    kiosk = _kiosk(db_session, alice, "Alice Print")
    a_paid_order(db_session, student, kiosk)

    response = _export(client, alice, kiosk)

    # Asserted before reading the body: "the email is not in this 404" is a
    # test that cannot fail, and one of those has already shipped here.
    assert response.status_code == 200
    body = response.text
    assert STUDENT_EMAIL not in body
    assert "Ravi Kumar" not in body


def test_an_unpaid_order_is_not_in_the_takings(client, db_session, alice, student):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    document = Document(
        user_id=student.id,
        original_filename="unpaid.pdf",
        page_count=2,
        original_path="originals/2026/08/unpaid.pdf",
        state=DocumentState.READY,
    )
    db_session.add(document)
    db_session.flush()
    place_order(
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

    response = _export(client, alice, kiosk)

    assert response.status_code == 200
    rows = [line for line in response.text.splitlines() if line]
    assert len(rows) == 1, "the header row and nothing else"


def test_the_window_is_honoured(client, db_session, alice, student):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    a_paid_order(db_session, student, kiosk)

    response = _export(
        client,
        alice,
        kiosk,
        until=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
    )

    assert response.status_code == 200
    rows = [line for line in response.text.splitlines() if line]
    assert len(rows) == 1, "the header row and nothing else"


def test_another_owners_kiosk_is_not_there_to_export(client, db_session, alice, bob):
    kiosk = _kiosk(db_session, alice, "Alice Print")

    response = _export(client, bob, kiosk)

    assert response.status_code == 404


def test_a_range_too_large_to_export_is_refused_rather_than_truncated(
    client, db_session, alice, student, monkeypatch
):
    """A silently truncated accounting export is a wrong number nobody can see.

    Refusing costs somebody a second request with a shorter range. Truncating
    costs them a reconciliation that will never balance.
    """
    monkeypatch.setattr(earnings_routes, "MAX_EXPORT_ROWS", 1)
    kiosk = _kiosk(db_session, alice, "Alice Print")
    a_paid_order(db_session, student, kiosk)
    a_paid_order(db_session, student, kiosk)

    response = _export(client, alice, kiosk)

    assert response.status_code == 400
    assert "shorter" in response.json()["detail"]


def test_the_filename_cannot_be_bent_by_a_kiosks_name(client, db_session, alice):
    """A shop's name is somebody else's text, and a header is a line-based format.

    The download is named by the kiosk's opaque id for that reason -- a name
    carrying a quote or a newline would otherwise rewrite the response headers.
    """
    kiosk = _kiosk(db_session, alice, 'Alice "Print"\r\nX-Injected: yes')

    disposition = _export(client, alice, kiosk).headers["content-disposition"]

    assert kiosk.public_id in disposition
    assert "\n" not in disposition and "\r" not in disposition
    assert "X-Injected" not in disposition


# ── admin is a wider scope here too ─────────────────────────────────────────
#
# The authz matrix declares every route in this router as {OWNER, ADMIN}, and
# the router's own guard says the same. Each route then narrowed it again to
# OWNER, so an admin was refused from a surface the matrix promised them --
# and `test_matrix_enforced` could not see it, because that test fires the
# audiences the matrix does *not* name and requires a refusal. Nothing was
# checking that a named audience gets through.


@pytest.fixture
def an_admin(db_session) -> User:
    return _user(db_session, "admin@example.com", Role.ADMIN)


def test_admin_sees_earnings_across_the_whole_estate(
    client, db_session, an_admin, alice, bob, student
):
    a_paid_order(db_session, student, _kiosk(db_session, alice, "Alice's"))
    a_paid_order(db_session, student, _kiosk(db_session, bob, "Bob's"))

    response = client.get("/v1/owner/earnings", headers=_auth(an_admin))

    assert response.status_code == 200
    assert response.json()["order_count"] == 2


def test_admin_sees_earnings_by_kiosk(client, db_session, an_admin, alice, bob, student):
    a_paid_order(db_session, student, _kiosk(db_session, alice, "Alice's"))
    a_paid_order(db_session, student, _kiosk(db_session, bob, "Bob's"))

    response = client.get("/v1/owner/earnings/by-kiosk", headers=_auth(an_admin))

    assert response.status_code == 200
    assert {row["kiosk_name"] for row in response.json()} == {"Alice's", "Bob's"}


def test_admin_reads_any_kiosks_orders(client, db_session, an_admin, alice, student):
    kiosk = _kiosk(db_session, alice, "Alice's")
    a_paid_order(db_session, student, kiosk)

    response = client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/orders", headers=_auth(an_admin)
    )

    assert response.status_code == 200
    assert len(response.json()) == 1


def test_admin_exports_any_kiosks_orders(client, db_session, an_admin, alice, student):
    kiosk = _kiosk(db_session, alice, "Alice's")
    a_paid_order(db_session, student, kiosk)

    response = client.get(
        f"/v1/owner/kiosks/{kiosk.public_id}/orders/export", headers=_auth(an_admin)
    )

    assert response.status_code == 200
    assert "order_id" in response.text


# ── the day series ──────────────────────────────────────────────────────────
#
# A real endpoint rather than a chart each console adds up for itself. The admin
# console built one client-side out of the order export -- which caps, buckets
# in UTC and knows nothing of refunds -- and the owner app was about to build a
# second. Two implementations of "what did this shop take on Tuesday" is the
# shape of every defect in the legacy audit.


def test_the_days_sum_to_the_figure_printed_above_them(
    client, db_session, alice, student
):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    a_paid_order(db_session, student, kiosk, pages=10)
    a_paid_order(db_session, student, kiosk, pages=20)

    series = client.get("/v1/owner/earnings/daily", headers=_auth(alice)).json()
    total = client.get("/v1/owner/earnings", headers=_auth(alice)).json()

    assert sum(Decimal(row["earnings"]["gross_inr"]) for row in series) == Decimal(
        total["gross_inr"]
    )
    assert sum(row["earnings"]["order_count"] for row in series) == total["order_count"]


def test_a_day_row_carries_the_same_four_numbers_as_the_total(
    client, db_session, alice, student
):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    a_paid_order(db_session, student, kiosk, pages=10)

    row = client.get("/v1/owner/earnings/daily", headers=_auth(alice)).json()[0]

    assert set(row) == {"day", "earnings"}
    assert set(row["earnings"]) == {
        "gross_inr",
        "refunded_inr",
        "net_inr",
        "order_count",
    }


def test_the_series_covers_only_this_owners_kiosks(
    client, db_session, alice, bob, student
):
    a_paid_order(db_session, student, _kiosk(db_session, bob, "Bob Print"), pages=50)
    _kiosk(db_session, alice, "Alice Print")

    series = client.get("/v1/owner/earnings/daily", headers=_auth(alice)).json()

    assert series == []


def test_one_shop_can_be_asked_about_on_its_own(client, db_session, alice, student):
    quiet = _kiosk(db_session, alice, "Quiet Print")
    busy = _kiosk(db_session, alice, "Busy Print")
    a_paid_order(db_session, student, busy, pages=10)

    assert client.get(
        f"/v1/owner/earnings/daily?kiosk_id={quiet.public_id}", headers=_auth(alice)
    ).json() == []
    assert (
        client.get(
            f"/v1/owner/earnings/daily?kiosk_id={busy.public_id}", headers=_auth(alice)
        ).json()[0]["earnings"]["gross_inr"]
        == "20.00"
    )


def test_another_owners_kiosk_is_not_found_rather_than_forbidden(
    client, db_session, alice, bob
):
    """A 403 would confirm the kiosk exists, which tells one shop owner
    something true about a competitor.

    Alice's own kiosk is asked for first, because a 404 is also what a route
    that does not exist answers -- and a test that cannot tell those apart
    passes before the feature is written."""
    mine = _kiosk(db_session, alice, "Alice Print")
    theirs = _kiosk(db_session, bob, "Bob Print")

    assert (
        client.get(
            f"/v1/owner/earnings/daily?kiosk_id={mine.public_id}", headers=_auth(alice)
        ).status_code
        == 200
    )
    response = client.get(
        f"/v1/owner/earnings/daily?kiosk_id={theirs.public_id}", headers=_auth(alice)
    )

    assert response.status_code == 404


def test_a_student_cannot_read_a_shops_day_series(client, db_session, alice, student):
    _kiosk(db_session, alice, "Alice Print")

    response = client.get("/v1/owner/earnings/daily", headers=_auth(student))

    assert response.status_code == 403


def test_admin_sees_the_whole_estate_day_by_day(
    client, db_session, an_admin, alice, bob, student
):
    a_paid_order(db_session, student, _kiosk(db_session, alice, "Alice Print"), pages=10)
    a_paid_order(db_session, student, _kiosk(db_session, bob, "Bob Print"), pages=20)

    series = client.get("/v1/owner/earnings/daily", headers=_auth(an_admin)).json()

    assert sum(row["earnings"]["order_count"] for row in series) == 2
