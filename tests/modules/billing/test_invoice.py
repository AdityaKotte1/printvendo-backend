"""The invoice an owner keeps for their books.

An owner pays Printvendo for the software, and a business that pays for
something needs a document saying what it paid for. The legacy app had one and
it was dropped in the rewire because the endpoint behind it no longer existed;
this is that document, built where the numbers are.

Two properties matter more than the layout:

**It exists only for money that arrived.** A subscription still waiting to be
paid for is a quote. Printing "TOTAL PAID" against money nobody has taken is
how a document ends up being waved at somebody as proof of a payment that never
happened.

**It does not change when you open it again.** An invoice number derived from
the subscription and an issue date read off the capture make the same document
every time. A number from a counter, or a date of `now`, would produce two
different papers for one payment -- and the whole point of a document is that
two people looking at it are looking at the same thing.
"""

import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pypdf import PdfReader

from app.modules.billing.invoice import (
    NOT_PAID_FOR,
    InvoiceParty,
    invoice_number,
    render_subscription_invoice,
)
from app.modules.billing.models import Plan, Subscription, SubscriptionStatus
from app.modules.identity.models import User

PAID_AT = datetime(2026, 8, 22, 9, 30, tzinfo=UTC)

PRINTVENDO = InvoiceParty(
    name="Printvendo",
    email="billing@printvendo.com",
    lines=("Printvendo Technologies", "Bengaluru, Karnataka"),
)


def text_of(pdf: bytes) -> str:
    """What a person reading the invoice would see.

    Read back rather than searched for in the raw bytes: reportlab compresses
    its content streams, so `b"Gupta Xerox" in pdf` is false for an invoice
    that plainly says so -- a test that passes only when the PDF is malformed.
    """
    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


@pytest.fixture
def owner(db_session) -> User:
    user = User(
        email="gupta@xerox.example",
        hashed_password="x",
        full_name="Ramesh Gupta",
    )
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def plan(db_session) -> Plan:
    plan = Plan(name="Pro", monthly_price=Decimal("1000.00"))
    db_session.add(plan)
    db_session.flush()
    return plan


def a_subscription(db_session, owner, plan, **kwargs) -> Subscription:
    defaults = dict(
        user_id=owner.id,
        plan_id=plan.id,
        duration_months=6,
        monthly_price_charged=Decimal("1000.00"),
        discount_percent=Decimal("10.00"),
        total_amount=Decimal("5400.00"),
        status=SubscriptionStatus.ACTIVE,
        starts_at=PAID_AT,
        expires_at=datetime(2027, 2, 18, 9, 30, tzinfo=UTC),
    )
    subscription = Subscription(**{**defaults, **kwargs})
    db_session.add(subscription)
    db_session.flush()
    return subscription


# ── what is on it ───────────────────────────────────────────────────────────


def test_the_invoice_names_the_owner_it_is_billed_to(db_session, owner, plan):
    """Their name and their address, because the person filing this needs to
    see who it was made out to without opening another screen."""
    subscription = a_subscription(db_session, owner, plan)

    text = text_of(
        render_subscription_invoice(
            subscription,
            plan_name=plan.name,
            billed_to=InvoiceParty(
                name="Ramesh Gupta",
                email=owner.email,
                lines=("Gupta Xerox", "12 MG Road, Bengaluru 560001"),
            ),
            billed_by=PRINTVENDO,
            paid_at=PAID_AT,
            payment_reference="pay_ABC123",
        )
    )

    assert "Ramesh Gupta" in text
    assert "gupta@xerox.example" in text
    assert "Gupta Xerox" in text
    assert "12 MG Road, Bengaluru 560001" in text


def test_the_invoice_says_what_was_bought_and_for_how_long(db_session, owner, plan):
    subscription = a_subscription(db_session, owner, plan)

    text = text_of(
        render_subscription_invoice(
            subscription,
            plan_name=plan.name,
            billed_to=InvoiceParty(name="Ramesh Gupta", email=owner.email),
            billed_by=PRINTVENDO,
            paid_at=PAID_AT,
            payment_reference="pay_ABC123",
        )
    )

    assert "Pro" in text
    assert "6 months" in text
    assert "1,000.00" in text  # a month
    assert "5,400.00" in text  # the total actually charged
    assert "10" in text  # the discount that explains the difference


def test_a_discount_is_shown_rather_than_folded_into_the_total(
    db_session, owner, plan
):
    """Otherwise the arithmetic on the page does not work: six months at a
    thousand is six thousand, and the document says five thousand four
    hundred with nothing accounting for the gap."""
    subscription = a_subscription(db_session, owner, plan)

    text = text_of(
        render_subscription_invoice(
            subscription,
            plan_name=plan.name,
            billed_to=InvoiceParty(name="Ramesh Gupta", email=owner.email),
            billed_by=PRINTVENDO,
            paid_at=PAID_AT,
            payment_reference=None,
        )
    )

    assert "6,000.00" in text  # what six months would have cost
    assert "5,400.00" in text  # what was charged


def test_the_payment_reference_is_on_it(db_session, owner, plan):
    """It is how a line on a bank statement is matched to this document."""
    subscription = a_subscription(db_session, owner, plan)

    text = text_of(
        render_subscription_invoice(
            subscription,
            plan_name=plan.name,
            billed_to=InvoiceParty(name="Ramesh Gupta", email=owner.email),
            billed_by=PRINTVENDO,
            paid_at=PAID_AT,
            payment_reference="pay_ABC123",
        )
    )

    assert "pay_ABC123" in text


# ── the document is the same document ───────────────────────────────────────


def test_the_invoice_number_is_derived_from_the_subscription(db_session, owner, plan):
    """Not from a counter. A counter would need to survive a rollback and be
    unique across the estate; the subscription id already is both, and an
    invoice that can be looked up by its own number is worth more than one that
    counts."""
    subscription = a_subscription(db_session, owner, plan)

    assert invoice_number(subscription).endswith(subscription.public_id.upper())
    assert invoice_number(subscription) == invoice_number(subscription)


def test_the_issue_date_is_when_the_money_arrived(db_session, owner, plan):
    """Not `now`, and not the day the term starts.

    An invoice whose date moves every time it is downloaded is not a document --
    two people looking at it are looking at different papers. The term here
    starts a fortnight after the money arrived, so a renderer reading the wrong
    field prints the wrong date rather than accidentally the right one.
    """
    subscription = a_subscription(
        db_session,
        owner,
        plan,
        starts_at=datetime(2026, 9, 5, 0, 0, tzinfo=UTC),
        expires_at=datetime(2027, 3, 5, 0, 0, tzinfo=UTC),
    )

    text = text_of(
        render_subscription_invoice(
            subscription,
            plan_name=plan.name,
            billed_to=InvoiceParty(name="Ramesh Gupta", email=owner.email),
            billed_by=PRINTVENDO,
            paid_at=PAID_AT,
            payment_reference="pay_ABC123",
        )
    )

    assert "22 Aug 2026" in text  # when it was paid
    assert "05 Sep 2026" in text  # and, separately, when the term starts


# ── only for money that arrived ─────────────────────────────────────────────


def test_a_subscription_nobody_has_paid_for_has_no_invoice(db_session, owner, plan):
    subscription = a_subscription(
        db_session, owner, plan, status=SubscriptionStatus.PENDING_PAYMENT
    )

    with pytest.raises(Exception) as raised:
        render_subscription_invoice(
            subscription,
            plan_name=plan.name,
            billed_to=InvoiceParty(name="Ramesh Gupta", email=owner.email),
            billed_by=PRINTVENDO,
            paid_at=None,
            payment_reference=None,
        )

    assert NOT_PAID_FOR in str(raised.value)


def test_a_trial_nobody_paid_for_has_no_invoice(db_session, owner, plan):
    """A granted trial is in force and cost nothing. An invoice for it would
    be a document saying money changed hands when none did."""
    subscription = a_subscription(
        db_session,
        owner,
        plan,
        total_amount=Decimal("0.00"),
        monthly_price_charged=Decimal("0.00"),
        free_until=datetime(2026, 9, 22, tzinfo=UTC),
    )

    # A capture time is passed deliberately: it is the *amount* that has to
    # refuse this, not the absence of a payment. Otherwise the trial case would
    # be caught by the branch above and this test would prove nothing.
    with pytest.raises(Exception) as raised:
        render_subscription_invoice(
            subscription,
            plan_name=plan.name,
            billed_to=InvoiceParty(name="Ramesh Gupta", email=owner.email),
            billed_by=PRINTVENDO,
            paid_at=PAID_AT,
            payment_reference=None,
        )

    assert NOT_PAID_FOR in str(raised.value)
