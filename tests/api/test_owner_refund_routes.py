"""An owner giving money back at their own shop.

The complaint arrives at the counter, not at head office: "it did not come
out". Until now the only refund route was admin-only, so the person standing in
front of the student had to email somebody.

**It is the same refund.** One use case, two doors -- the admin's, which reaches
every order, and this one, which reaches the orders at the kiosks the caller
holds. The old backend had a refund in `kiosk.py` and a second in `refunds.py`
with different ideas about whose Razorpay collects, and that is how student
money went to the wrong account.

**The scope is the only difference.** An order at a kiosk the caller does not
hold is 404, byte-identical to an order that never existed -- and so is an order
at one of their *other* kiosks asked for under this kiosk's path, because the
path is the claim being checked.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_notifier, get_razorpay, get_secret, get_secret_box
from app.core.config import Settings
from app.core.crypto import SecretBox
from app.core.notifier import NullNotifier
from app.core.security import TokenType, create_token
from app.main import create_app
from app.modules.identity import repository as identity_repo
from app.modules.identity.models import User
from app.modules.identity.roles import Role
from app.modules.kiosks.enums import AssignmentRole, KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskAssignment, KioskPaper
from app.modules.ops import entries_for
from app.modules.orders.models import PaymentMethod
from app.modules.orders.service import (
    RequestedDocument,
    mark_paid,
    pay_with_wallet,
    place_order,
)
from app.modules.payments import PaymentKind, set_keys
from app.modules.payments.charges import Credentials, open_checkout, record_capture
from app.modules.payments.gate import Gateway
from app.modules.printing import PrintOptions
from app.modules.printing.models import Document, DocumentState
from app.modules.wallet import EntryKind, balance_of, credit

SECRET = "s" * 32
BOX_KEY = Fernet.generate_key().decode()
BOX = SecretBox(BOX_KEY)

OWNER_KEY_ID = "rzp_alice_live"
PLATFORM_KEY_ID = "rzp_test_platform"

SETTINGS = Settings(
    ENV="dev",
    DATABASE_URL="postgresql+psycopg://u:p@localhost:5432/pv",
    REDIS_URL="redis://localhost:6379/0",
    JWT_SECRET_KEY=SECRET,
    SECRETS_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    RAZORPAY_KEY_ID="rzp_test_platform",
    RAZORPAY_KEY_SECRET="platform_secret",
    CORS_ORIGINS="https://owner.printvendo.com",
)


class FakeRazorpay:
    def __init__(self) -> None:
        self.refunds: list[tuple[str, int, str | None]] = []

    def create_order(self, *, amount_paise: int, receipt: str, credentials) -> str:
        # Unique per receipt: `razorpay_order_id` is a unique column, and two
        # checkouts in one test are an ordinary thing to want.
        return f"order_{receipt}"

    def refund(
        self,
        *,
        razorpay_payment_id: str,
        amount_paise: int,
        credentials=None,
        **kwargs,
    ) -> str:
        # The key id is recorded, because whose account a refund is issued
        # against is the thing these tests are about.
        key_id = getattr(credentials, "key_id", None)
        self.refunds.append((razorpay_payment_id, amount_paise, key_id))
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
    app.dependency_overrides[get_secret_box] = lambda: BOX
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
    """An owner who collects into her own Razorpay account.

    That is not decoration on these tests: it is the only shape in which an
    owner may refund at all, because the money has to come back out of the
    account it went into.
    """
    owner = _user(db_session, "alice@example.com", Role.OWNER)
    set_keys(
        db_session,
        owner.id,
        key_id=OWNER_KEY_ID,
        key_secret="alice_secret_value",
        box=BOX,
    )
    db_session.flush()
    return owner


@pytest.fixture
def bob(db_session) -> User:
    return _user(db_session, "bob@example.com", Role.OWNER)


@pytest.fixture
def an_admin(db_session) -> User:
    return _user(db_session, "ops@printvendo.com", Role.ADMIN)


@pytest.fixture
def student(db_session) -> User:
    person = _user(db_session, "student@example.com", Role.STUDENT)
    credit(
        db_session,
        user_id=person.id,
        amount=Decimal("500.00"),
        kind=EntryKind.TOPUP,
        reference="owner_refund_seed",
    )
    return person


def a_paid_order(db_session, student: User, kiosk: Kiosk, *, pages: int = 10):
    document = Document(
        user_id=student.id,
        original_filename="jammed.pdf",
        page_count=pages,
        original_path=f"originals/2026/08/{pages}-{kiosk.id}.pdf",
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


def a_card_order(db_session, student, kiosk, collector, *, pages: int = 10):
    """A gateway order captured into `collector`'s Razorpay account.

    `collecting_user_id` is the payment gate's answer written down at checkout.
    Everything about a refund is read back off it -- where the money may go,
    and whose keys it is issued with -- so setting it here is setting the whole
    situation under test.
    """
    document = Document(
        user_id=student.id,
        original_filename="card.pdf",
        page_count=pages,
        original_path=f"originals/2026/08/card-{pages}-{kiosk.id}.pdf",
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
        method=PaymentMethod.GATEWAY,
    )
    payment = open_checkout(
        db_session,
        FakeRazorpay(),
        user_id=student.id,
        kind=PaymentKind.PRINT_ORDER,
        amount=order.total_inr,
        receipt=order.public_id,
        kiosk=kiosk,
        credentials=Credentials(OWNER_KEY_ID, "alice_secret_value"),
        gateway=Gateway.OWNER_GATEWAY,
        collecting_user_id=collector.id if collector else None,
        order_id=order.id,
    )
    record_capture(db_session, payment, razorpay_payment_id=f"pay_card_{order.id}")
    mark_paid(db_session, order, reference=payment.razorpay_payment_id)
    return order


def _refund(client, auth, kiosk, order, **body):
    return client.post(
        f"/v1/owner/kiosks/{kiosk.public_id}/orders/{order.public_id}/refund",
        headers=auth,
        json={"idempotency_key": "owner-refund-0001", **body},
    )


# ── the shop can put it right ───────────────────────────────────────────────


def test_an_owner_refunds_an_order_at_their_own_shop(
    client, db_session, alice, student, razorpay
):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    response = _refund(client, _auth(alice), kiosk, order, reason="Printer jammed")

    assert response.status_code == 201, response.text
    # Sent to Razorpay, against the payment it reverses.
    assert [pay_id for pay_id, _, _ in razorpay.refunds] == [f"pay_card_{order.id}"]


def test_the_default_is_everything_still_owed(client, db_session, alice, student):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    body = _refund(client, _auth(alice), kiosk, order).json()

    # The whole of what was captured, gateway fee included -- the student paid
    # it, so the student gets it back.
    assert body["amount_inr"] == str(order.total_inr)
    assert body["order_id"] == order.public_id


def test_a_partial_refund_is_possible(client, db_session, alice, student):
    """Three of five documents came out, so two fifths goes back."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    body = _refund(client, _auth(alice), kiosk, order, amount_inr="8.00").json()

    assert body["amount_inr"] == "8.00"
    assert body["refunded_total_inr"] == "8.00"


def test_the_same_key_twice_returns_the_same_refund(
    client, db_session, alice, student, razorpay
):
    """The rule the refund service exists to enforce, on this door too: a
    request that timed out is retried and must not move money twice."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    first = _refund(client, _auth(alice), kiosk, order).json()
    second = _refund(client, _auth(alice), kiosk, order).json()

    assert second["id"] == first["id"]
    # And Razorpay was asked once, not twice.
    assert len(razorpay.refunds) == 1


def test_refunding_more_than_was_paid_is_refused(client, db_session, alice, student):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    assert _refund(
        client, _auth(alice), kiosk, order, amount_inr="100.00"
    ).status_code == 409


def test_a_balance_payment_cannot_be_sent_back_to_a_card(
    client, db_session, an_admin, alice, student
):
    """There is no gateway payment to reverse.

    Asked through the admin door, because an owner cannot reach a balance
    payment at all: nothing was collected into their account, so it is not
    their takings to give back.
    """
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_paid_order(db_session, student, kiosk)

    response = client.post(
        f"/v1/admin/orders/{order.public_id}/refund",
        headers=_auth(an_admin),
        json={"idempotency_key": "admin-source-0001", "destination": "source"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "That payment did not go through a gateway."


# ── scope ───────────────────────────────────────────────────────────────────


def test_another_owners_order_is_not_found_rather_than_forbidden(
    client, db_session, alice, bob, student
):
    """A 403 would confirm the order exists at a kiosk Alice cannot see.

    Alice's own refund is asked for first: a 404 is also what a route that does
    not exist answers, and a test that cannot tell those apart passes before
    the feature is written.
    """
    mine = _kiosk(db_session, alice, "Alice Print")
    theirs = _kiosk(db_session, bob, "Bob Print")
    my_order = a_card_order(db_session, student, mine, alice)
    their_order = a_card_order(db_session, student, theirs, bob, pages=6)

    assert _refund(client, _auth(alice), mine, my_order).status_code == 201

    response = _refund(
        client,
        _auth(alice),
        theirs,
        their_order,
        idempotency_key="owner-refund-0002",
    )

    assert response.status_code == 404


def test_an_order_from_a_different_shop_is_not_refundable_under_this_one(
    client, db_session, alice, student
):
    """Both shops are Alice's, so this is not a scope refusal -- it is the path
    being a claim. An order id that belonged to whichever kiosk happened to be
    named would make the kiosk in the URL decoration."""
    one = _kiosk(db_session, alice, "Alice Print One")
    two = _kiosk(db_session, alice, "Alice Print Two")
    order_at_two = a_card_order(db_session, student, two, alice)

    response = _refund(client, _auth(alice), one, order_at_two)

    assert response.status_code == 404
    # The sentence, not just the code: a route that does not exist answers 404
    # too, and a test that cannot tell those apart passes before the feature is
    # written.
    assert response.json()["detail"] == "That order does not exist."


def test_an_unpaid_order_has_nothing_to_give_back(client, db_session, alice, student):
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
    order = place_order(
        db_session,
        user=student,
        kiosk=kiosk,
        requests=[
            RequestedDocument(document=document, options=PrintOptions.create(total_pages=2))
        ],
        method=PaymentMethod.WALLET,
    )

    response = _refund(client, _auth(alice), kiosk, order)

    assert response.status_code == 404
    assert response.json()["detail"] == (
        "Nothing has been paid for that order, so there is nothing to give back."
    )


def test_a_student_cannot_refund_their_own_order(client, db_session, alice, student):
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    response = _refund(client, _auth(student), kiosk, order)

    assert response.status_code == 403


def test_an_admin_is_refused_at_the_shops_own_door(
    client, db_session, an_admin, alice, student
):
    """The one route in the owner surface where admin is not alongside.

    Everywhere else admin is a wider kiosk scope through the same route. This
    one is not about scope: it is a shop giving back its own takings out of its
    own Razorpay, and an admin has collected nothing. Refusing at the door says
    that; letting them in and refusing at the money would read as a bug.
    Platform money goes back through the admin door instead.
    """
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    assert _refund(client, _auth(an_admin), kiosk, order).status_code == 403


# ── the trail ───────────────────────────────────────────────────────────────


def test_a_refund_is_recorded_against_whoever_issued_it(
    client, db_session, alice, student
):
    """Money going back is exactly what somebody has to answer for later, and
    an owner refunding their own shop's takings is the case where that matters
    most."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    _refund(client, _auth(alice), kiosk, order, reason="Printer jammed")

    entry = entries_for(db_session, action="payment.refunded")[0]
    assert entry.actor_user_id == alice.id
    assert entry.after["order_id"] == order.public_id
    assert entry.note == "Printer jammed"


def test_a_retried_refund_is_recorded_once(client, db_session, alice, student):
    """The retry gives back the refund that already happened, so the trail must
    not gain a second entry for it.

    Found by using the route rather than reading it: two identical requests
    moved ₹1 once and wrote `payment.refunded` twice. An operator reading the
    audit sees two refunds of ₹1 where one was made — and the trail is the only
    record there is, because owners are paid directly and there is no
    settlement run in which the discrepancy would surface.
    """
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    _refund(client, _auth(alice), kiosk, order)
    _refund(client, _auth(alice), kiosk, order)

    assert len(entries_for(db_session, action="payment.refunded")) == 1


# ── whose money an owner may give back ──────────────────────────────────────
#
# One rule, and two consequences that fall out of it rather than being enforced
# a second time: **an owner refunds money their own account collected.**
#
#   * It can only go back to the source. Crediting a Printvendo balance is the
#     platform promising to honour rupees it holds, and against money that went
#     straight into a shop's own Razorpay there is nothing behind that promise.
#     `_may_go_to_wallet` already refuses it; the owner request type has no
#     field to ask with, so the question cannot even be posed.
#   * It is issued against the **owner's** Razorpay keys, never the platform's,
#     because `credentials_for_payment` reads the same `collecting_user_id`
#     this rule checks. The two cannot disagree -- they are one column.


def test_an_owner_refunds_out_of_their_own_razorpay_account(
    client, db_session, alice, student, razorpay
):
    """The money goes back out of the account it went into. Issuing it against
    the platform's keys is a request Razorpay refuses outright, because the
    payment id is not theirs -- it turns a refund into a support ticket, and the
    shop has no way to tell."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    response = _refund(client, _auth(alice), kiosk, order, reason="Printer jammed")

    assert response.status_code == 201, response.text
    assert response.json()["destination"] == "source"
    assert [key_id for _, _, key_id in razorpay.refunds] == [OWNER_KEY_ID]


def test_an_owner_cannot_refund_money_printvendo_collected(
    client, db_session, alice, student
):
    """A PLATFORM kiosk's takings are not the shop's to give back. Letting an
    owner do it would send platform money out of the platform's account on a
    decision nobody at the platform made -- and it is the only way an owner
    could reach a wallet refund, which is the other half of the same rule."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, None)

    response = _refund(client, _auth(alice), kiosk, order)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "That payment was collected by Printvendo rather than by your own "
        "account, so it cannot be refunded from here. Ask Printvendo to "
        "refund it."
    )


def test_an_owner_cannot_refund_an_order_paid_from_a_balance(
    client, db_session, alice, student
):
    """Balance money is Printvendo's to give back for the same reason: nothing
    was ever collected into the shop's account, so there is nothing there to
    reverse. Caught by the same check rather than by a second one -- a wallet
    payment has no collecting account either."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_paid_order(db_session, student, kiosk)

    response = _refund(client, _auth(alice), kiosk, order)

    assert response.status_code == 409


def test_the_owner_request_has_no_way_to_ask_for_the_wallet(
    client, db_session, alice, student, razorpay
):
    """Not refused -- unaskable. `OwnerRefundRequest` has no destination field,
    so a body carrying one is a body carrying a word nobody reads. A refusal
    would be a second copy of the rule, and the kind that gets relaxed."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, alice)

    response = _refund(client, _auth(alice), kiosk, order, destination="wallet")

    assert response.status_code == 201, response.text
    assert response.json()["destination"] == "source"


def test_an_admin_may_still_refund_what_printvendo_collected(
    client, db_session, an_admin, alice, student, razorpay
):
    """The admin door is unchanged, and this is what it is for: the money the
    owner surface refuses. It goes back through the platform's own keys."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_card_order(db_session, student, kiosk, None)

    response = client.post(
        f"/v1/admin/orders/{order.public_id}/refund",
        headers=_auth(an_admin),
        json={"idempotency_key": "admin-refund-0001"},
    )

    assert response.status_code == 201, response.text
    assert [key_id for _, _, key_id in razorpay.refunds] == [PLATFORM_KEY_ID]


def test_an_admin_may_still_send_platform_money_to_a_balance(
    client, db_session, an_admin, alice, student
):
    """Wallet is still a legal destination for money Printvendo collected --
    the rule that removed it is about *owners*, not about the destination
    table, which is unchanged."""
    kiosk = _kiosk(db_session, alice, "Alice Print")
    order = a_paid_order(db_session, student, kiosk)
    before = balance_of(db_session, user_id=student.id)

    response = client.post(
        f"/v1/admin/orders/{order.public_id}/refund",
        headers=_auth(an_admin),
        json={"idempotency_key": "admin-refund-0002"},
    )

    assert response.status_code == 201, response.text
    assert balance_of(db_session, user_id=student.id) == before + Decimal("20.00")
