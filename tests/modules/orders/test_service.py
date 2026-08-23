"""Placing an order, and paying for it in one commit.

This is the module that makes "paid but never printed" unreachable. The old
backend marked a payment PAID and created no print job, leaving the caller to
remember a second request; every test here exists to keep that impossible.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import BadRequest, Conflict
from app.modules.kiosks.enums import KioskType, OnboardingStage
from app.modules.kiosks.models import Kiosk, KioskPaper
from app.modules.orders.models import ItemKind, OrderState, PaymentMethod
from app.modules.orders.service import (
    ALREADY_PAID,
    ORDER_EXPIRED,
    ORDER_LIFETIME,
    RequestedDocument,
    expire_stale_orders,
    mark_paid,
    place_order,
    sheets_available,
)
from app.modules.printing import PrintOptions
from app.modules.printing.models import (
    Document,
    DocumentState,
    PrintTask,
    TaskState,
)


@pytest.fixture
def user(db_session):
    from app.modules.identity.models import User

    user = User(email="orders@example.com", hashed_password="x")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def kiosk(db_session):
    kiosk = Kiosk(
        name="Orders Shop",
        kiosk_type=KioskType.PLATFORM,
        onboarding_stage=OnboardingStage.LIVE,
        accepts_wallet=True,
        price_bw_single=Decimal("2.00"),
        price_bw_double=Decimal("3.00"),
        price_color_single=Decimal("10.00"),
        price_color_double=Decimal("18.00"),
    )
    db_session.add(kiosk)
    db_session.flush()
    db_session.add(KioskPaper(kiosk_id=kiosk.id, capacity=500, used=0))
    db_session.flush()
    return kiosk


def make_document(db_session, user, *, pages: int = 10) -> Document:
    # READY is what create_document leaves behind; a raw row would default to
    # UPLOADED and be refused for a reason that has nothing to do with the test.
    doc = Document(
        user_id=user.id,
        original_filename="essay.pdf",
        page_count=pages,
        original_path="originals/2026/08/test.pdf",
        state=DocumentState.READY,
    )
    db_session.add(doc)
    db_session.flush()
    return doc


def request_for(document: Document, **kwargs) -> RequestedDocument:
    defaults = {"colour": False, "duplex": False, "copies": 1}
    return RequestedDocument(
        document=document,
        options=PrintOptions.create(
            total_pages=document.page_count, **{**defaults, **kwargs}
        ),
    )


# ── placing ─────────────────────────────────────────────────────────────────


def test_an_order_is_placed_awaiting_payment(db_session, user, kiosk):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.GATEWAY,
    )

    assert order.state is OrderState.AWAITING_PAYMENT
    assert order.public_id.startswith("ord_")


def test_the_order_stores_what_it_quoted(db_session, user, kiosk):
    """Not recomputed at payment time: a price band edited in between must not
    change what somebody is charged after they agreed to a number."""
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.GATEWAY,
    )

    assert order.subtotal_inr == Decimal("20.00")
    assert order.fee_inr == Decimal("0.40")
    assert order.total_inr == Decimal("20.40")


def test_a_wallet_order_carries_no_fee(db_session, user, kiosk):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )

    assert order.fee_inr == Decimal("0.00")
    assert order.total_inr == Decimal("20.00")


def test_every_document_becomes_an_item_in_the_students_order(db_session, user, kiosk):
    first = make_document(db_session, user, pages=4)
    second = make_document(db_session, user, pages=6)

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(second), request_for(first)],
        method=PaymentMethod.WALLET,
    )

    assert [item.position for item in order.items] == [0, 1]
    assert [item.document_id for item in order.items] == [second.id, first.id]
    assert all(item.kind is ItemKind.DOCUMENT for item in order.items)


def test_an_item_keeps_the_settings_that_were_paid_for(db_session, user, kiosk):
    document = make_document(db_session, user)

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(document, colour=True, duplex=True, copies=2)],
        method=PaymentMethod.WALLET,
    )

    item = order.items[0]
    assert (item.colour, item.duplex, item.copies) == (True, True, 2)
    # A page is one side of a sheet: ten pages, two copies, twenty sides, ten
    # sheets printed on both sides -- and the money follows the sheets.
    assert item.impressions == 20
    assert item.sheets == 10
    assert item.amount_inr == Decimal("180.00")  # 10 x 18.00


def test_an_order_records_which_gateway_collects(db_session, user, kiosk):
    """Recorded once, from the one gate, so a refund months later goes back the
    way the money came rather than being re-derived from a kiosk whose
    configuration has changed since."""
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.GATEWAY,
    )

    assert order.gateway == "platform_gateway"


def test_an_order_expires_if_it_is_never_paid(db_session, user, kiosk):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.GATEWAY,
    )

    assert order.expires_at is not None
    assert order.expires_at > datetime.now(UTC)


def test_an_order_with_nothing_in_it_is_refused(db_session, user, kiosk):
    with pytest.raises(BadRequest):
        place_order(
            db_session, user=user, kiosk=kiosk, requests=[], method=PaymentMethod.WALLET
        )


def test_someone_elses_document_cannot_be_ordered(db_session, user, kiosk):
    """The document id is opaque, but an aggregate that trusted it would still
    let a guessed id print somebody else's coursework."""
    from app.modules.identity.models import User

    stranger = User(email="stranger@example.com", hashed_password="x")
    db_session.add(stranger)
    db_session.flush()
    theirs = make_document(db_session, stranger)

    with pytest.raises(BadRequest):
        place_order(
            db_session,
            user=user,
            kiosk=kiosk,
            requests=[request_for(theirs)],
            method=PaymentMethod.WALLET,
        )


def test_a_document_whose_file_is_gone_cannot_be_ordered(db_session, user, kiosk):
    document = make_document(db_session, user)
    document.state = DocumentState.EXPIRED
    db_session.flush()

    with pytest.raises(BadRequest):
        place_order(
            db_session,
            user=user,
            kiosk=kiosk,
            requests=[request_for(document)],
            method=PaymentMethod.WALLET,
        )


# ── the gate decides whether an order may exist ─────────────────────────────


def test_a_closed_kiosk_cannot_take_an_order(db_session, user, kiosk):
    """A SOLD kiosk with no keys and no subscription: the gate says CLOSED, and
    an order there would take a student's money with nowhere to send it."""
    kiosk.kiosk_type = KioskType.SOLD
    db_session.flush()

    with pytest.raises(BadRequest):
        place_order(
            db_session,
            user=user,
            kiosk=kiosk,
            requests=[request_for(make_document(db_session, user))],
            method=PaymentMethod.GATEWAY,
        )


def test_wallet_cannot_be_spent_where_the_platform_does_not_collect(
    db_session, user, kiosk
):
    """Top-ups land in the platform's Razorpay. Spending that balance at an
    owner-gateway kiosk would have the platform keep the cash while the owner
    prints for free -- the leak this rewrite exists to close."""
    kiosk.accepts_wallet = False
    db_session.flush()

    with pytest.raises(BadRequest):
        place_order(
            db_session,
            user=user,
            kiosk=kiosk,
            requests=[request_for(make_document(db_session, user))],
            method=PaymentMethod.WALLET,
        )


# ── paper ───────────────────────────────────────────────────────────────────


def test_availability_is_the_tray_minus_the_work_already_queued(
    db_session, user, kiosk
):
    assert sheets_available(db_session, kiosk) == 500

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user, pages=100))],
        method=PaymentMethod.WALLET,
    )
    mark_paid(db_session, order, reference="wallet:1")

    assert sheets_available(db_session, kiosk) == 400


def test_a_kiosk_without_enough_paper_refuses_the_order(db_session, user, kiosk):
    """Refused here rather than accepted and failed at the printer, which is
    what the old backend did -- it charged first and discovered the tray after."""
    document = make_document(db_session, user, pages=600)

    with pytest.raises(BadRequest) as exc:
        place_order(
            db_session,
            user=user,
            kiosk=kiosk,
            requests=[request_for(document)],
            method=PaymentMethod.WALLET,
        )

    assert "paper" in str(exc.value).lower()


def test_an_unpaid_order_holds_no_paper(db_session, user, kiosk):
    """The operator's call, and its consequence stated rather than discovered.

    A basket somebody opened and wandered away from does not stop the next
    student printing. So two orders for the same last sheets are both accepted,
    and the second job waits BLOCKED at the device until somebody refills --
    visible and recoverable, unlike a busy kiosk refusing work it could do.
    """
    place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user, pages=300))],
        method=PaymentMethod.WALLET,
    )

    assert sheets_available(db_session, kiosk) == 500

    second = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user, pages=300))],
        method=PaymentMethod.WALLET,
    )
    assert second.state is OrderState.AWAITING_PAYMENT


def test_paper_promised_to_a_queued_task_is_not_available_either(
    db_session, user, kiosk
):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user, pages=100))],
        method=PaymentMethod.WALLET,
    )
    mark_paid(db_session, order, reference="wallet:1")

    # The order is no longer awaiting payment, so its own reservation is gone --
    # but its print task now holds the same paper. It must not be double counted
    # and it must not be released.
    assert sheets_available(db_session, kiosk) == 400


# ── paying: one commit ──────────────────────────────────────────────────────


def test_paying_creates_every_print_task(db_session, user, kiosk):
    first = make_document(db_session, user, pages=4)
    second = make_document(db_session, user, pages=6)
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(first), request_for(second)],
        method=PaymentMethod.WALLET,
    )

    tasks = mark_paid(db_session, order, reference="wallet:1")

    assert order.state is OrderState.PAID
    assert order.paid_at is not None
    assert [t.document_id for t in tasks] == [first.id, second.id]
    assert all(t.state is TaskState.QUEUED for t in tasks)


def test_the_tasks_carry_the_settings_that_were_paid_for(db_session, user, kiosk):
    document = make_document(db_session, user)
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(document, colour=True, duplex=True, copies=2)],
        method=PaymentMethod.WALLET,
    )

    task = mark_paid(db_session, order, reference="wallet:1")[0]

    assert (task.colour, task.duplex, task.copies) == (True, True, 2)
    assert task.predicted_sheets == 10


def test_tasks_go_to_the_kiosk_the_order_was_placed_at(db_session, user, kiosk):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )

    task = mark_paid(db_session, order, reference="wallet:1")[0]

    assert task.kiosk_id == kiosk.id


def test_the_payment_reference_is_recorded(db_session, user, kiosk):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )

    mark_paid(db_session, order, reference="pay_ABC123")

    assert order.payment_reference == "pay_ABC123"


def test_paying_twice_does_not_print_twice(db_session, user, kiosk):
    """A retried webhook, or a student who pressed pay twice. The second attempt
    must not produce a second set of tasks."""
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )
    mark_paid(db_session, order, reference="wallet:1")

    with pytest.raises(Conflict) as exc:
        mark_paid(db_session, order, reference="wallet:1")

    # The sentence matters: a webhook retrying after a timeout should learn the
    # order is already paid, which is benign, rather than that it is "no longer
    # open", which reads like something went wrong and invites a support ticket.
    assert str(exc.value) == ALREADY_PAID
    assert db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).count() == 1


def test_an_expired_order_cannot_be_paid(db_session, user, kiosk):
    """Its paper has been released to somebody else by now."""
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )
    order.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    with pytest.raises(Conflict) as exc:
        mark_paid(db_session, order, reference="wallet:1")

    assert str(exc.value) == ORDER_EXPIRED
    assert db_session.query(PrintTask).count() == 0


def test_a_failure_while_queueing_leaves_the_order_unpaid(
    db_session, user, kiosk, monkeypatch
):
    """The whole point of the aggregate.

    If task creation fails, the payment must not stand. Modelled with a
    savepoint, because that is what the request transaction does in production:
    `get_db` rolls back on any exception, and the PAID flag has to go with it.
    A plain rollback here would discard the placed order too and prove nothing.
    """
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("the queue is on fire")

    monkeypatch.setattr("app.modules.orders.service.enqueue_task", _explode)

    savepoint = db_session.begin_nested()
    with pytest.raises(RuntimeError):
        mark_paid(db_session, order, reference="wallet:1")
    savepoint.rollback()
    db_session.expire_all()

    assert order.state is OrderState.AWAITING_PAYMENT
    assert order.paid_at is None
    assert order.payment_reference is None
    assert db_session.query(PrintTask).count() == 0


# ── expiry sweep ────────────────────────────────────────────────────────────


def test_the_sweep_expires_orders_nobody_paid_for(db_session, user, kiosk):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.GATEWAY,
    )
    order.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    expired = expire_stale_orders(db_session)

    assert order in expired
    assert order.state is OrderState.EXPIRED


def test_the_sweep_leaves_a_paid_order_alone(db_session, user, kiosk):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )
    mark_paid(db_session, order, reference="wallet:1")
    order.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    expire_stale_orders(db_session)

    assert order.state is OrderState.PAID


def test_the_sweep_leaves_an_order_that_is_still_open(db_session, user, kiosk):
    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.GATEWAY,
    )

    expire_stale_orders(db_session)

    assert order.state is OrderState.AWAITING_PAYMENT


def test_the_order_lifetime_is_long_enough_to_actually_pay(db_session):
    """A student on a phone, opening a payment app and coming back."""
    assert ORDER_LIFETIME >= timedelta(minutes=10)


# ── an order follows its prints ─────────────────────────────────────────────


def _funded(db_session, user, reference: str):
    """Enough balance to pay for the orders these tests place."""
    from app.modules.wallet import EntryKind, credit

    credit(
        db_session,
        user_id=user.id,
        amount=Decimal("500.00"),
        kind=EntryKind.TOPUP,
        reference=reference,
    )


def test_an_order_is_dispatched_once_a_device_takes_it(db_session, user, kiosk):
    """`DISPATCHED`, `COMPLETED` and `PARTIALLY_FAILED` existed and nothing ever
    set them, so an order stayed PAID for ever and the student's screen said
    "queued" after the print was already in their hand."""
    from app.modules.orders.service import pay_with_wallet, refresh_order_state
    from app.modules.printing.models import TaskState

    _funded(db_session, user, "progress_0")






    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)

    task = db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).one()
    task.state = TaskState.PRINTING
    db_session.flush()

    refresh_order_state(db_session, document_id=task.document_id, kiosk_id=kiosk.id)

    assert order.state is OrderState.DISPATCHED


def test_an_order_completes_when_every_document_has_printed(db_session, user, kiosk):
    from app.modules.orders.service import pay_with_wallet, refresh_order_state
    from app.modules.printing.models import TaskState

    _funded(db_session, user, "progress_1")

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[
            request_for(make_document(db_session, user)),
            request_for(make_document(db_session, user)),
        ],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)

    tasks = db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).all()
    for task in tasks:
        task.state = TaskState.PRINTED
    db_session.flush()

    refresh_order_state(db_session, document_id=tasks[0].document_id, kiosk_id=kiosk.id)

    assert order.state is OrderState.COMPLETED


def test_an_order_with_one_left_to_print_is_not_complete(db_session, user, kiosk):
    """Two files, one printed: the student is still waiting, and an order that
    said COMPLETED would take it off their screen."""
    from app.modules.orders.service import pay_with_wallet, refresh_order_state
    from app.modules.printing.models import TaskState

    _funded(db_session, user, "progress_2")

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[
            request_for(make_document(db_session, user)),
            request_for(make_document(db_session, user)),
        ],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)

    tasks = db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).all()
    tasks[0].state = TaskState.PRINTED
    db_session.flush()

    refresh_order_state(db_session, document_id=tasks[0].document_id, kiosk_id=kiosk.id)

    assert order.state is OrderState.DISPATCHED


def test_some_printed_and_some_failed_is_partially_failed(db_session, user, kiosk):
    """A real outcome rather than an error: the student is owed the difference,
    and an operator has to be able to see which orders those are."""
    from app.modules.orders.service import pay_with_wallet, refresh_order_state
    from app.modules.printing.models import TaskState

    _funded(db_session, user, "progress_3")

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[
            request_for(make_document(db_session, user)),
            request_for(make_document(db_session, user)),
        ],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)

    tasks = db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).all()
    tasks[0].state = TaskState.PRINTED
    tasks[1].state = TaskState.FAILED
    db_session.flush()

    refresh_order_state(db_session, document_id=tasks[0].document_id, kiosk_id=kiosk.id)

    assert order.state is OrderState.PARTIALLY_FAILED


def test_a_refunded_order_is_left_alone(db_session, user, kiosk):
    """A late report from a device must not resurrect an order somebody has
    already been given their money back for."""
    from app.modules.orders.service import pay_with_wallet, refresh_order_state
    from app.modules.printing.models import TaskState

    _funded(db_session, user, "progress_4")

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)
    order.state = OrderState.REFUNDED
    db_session.flush()

    task = db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).one()
    task.state = TaskState.PRINTED
    db_session.flush()
    refresh_order_state(db_session, document_id=task.document_id, kiosk_id=kiosk.id)

    assert order.state is OrderState.REFUNDED


def test_an_order_shows_as_printing_while_the_printer_works(db_session, user, kiosk):
    """Queued, printing, printed -- and the middle one is the point.

    An order that jumped from paid straight to completed showed a student
    "queued" for the whole print and then "printed", which is two of the three
    states they were promised.
    """
    from app.modules.orders.service import pay_with_wallet, refresh_order_state
    from app.modules.printing.models import TaskState

    _funded(db_session, user, "progress_printing")

    order = place_order(
        db_session,
        user=user,
        kiosk=kiosk,
        requests=[request_for(make_document(db_session, user))],
        method=PaymentMethod.WALLET,
    )
    pay_with_wallet(db_session, order)
    assert order.state is OrderState.PAID, "claimed but not yet on the printer"

    task = db_session.query(PrintTask).filter_by(kiosk_id=kiosk.id).one()
    task.state = TaskState.PRINTING
    db_session.flush()

    refresh_order_state(db_session, document_id=task.document_id, kiosk_id=kiosk.id)

    assert order.state is OrderState.DISPATCHED
