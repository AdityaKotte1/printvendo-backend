"""The receipt a student downloads.

Two properties matter more than the layout: it exists only for money that was
actually taken, and it is produced from the same view the screens read -- so it
cannot show a field the caller was never allowed to see.
"""

import io
from datetime import UTC, datetime
from decimal import Decimal

from pypdf import PdfReader

from app.modules.orders.invoice import render_invoice
from app.modules.orders.models import OrderState
from app.modules.orders.views import OrderLineView, OrderView


def _line(**kwargs) -> OrderLineView:
    defaults = dict(
        document_id="doc_1",
        filename="Assignment.pdf",
        kind="document",
        colour=False,
        duplex=True,
        copies=1,
        page_range=None,
        page_count=10,
        sheets=5,
        amount_inr=Decimal("15.00"),
    )
    return OrderLineView(**{**defaults, **kwargs})


def _view(**kwargs) -> OrderView:
    defaults = dict(
        id="ord_abc123",
        kiosk_id="ksk_xyz",
        kiosk_name="Campus Print",
        state=OrderState.PAID,
        payment_method="wallet",
        subtotal_inr=Decimal("15.00"),
        fee_inr=Decimal("0.00"),
        total_inr=Decimal("15.00"),
        expires_at=None,
        paid_at=datetime(2026, 8, 22, 9, 30, tzinfo=UTC),
        refunded_at=None,
        created_at=datetime(2026, 8, 22, 9, 25, tzinfo=UTC),
        items=[_line()],
    )
    return OrderView(**{**defaults, **kwargs})


def text_of(pdf: bytes) -> str:
    """What a person reading the receipt would see.

    Read back rather than searched for in the raw bytes: reportlab compresses
    its content streams, so `b"ord_abc123" in pdf` is false for a receipt that
    plainly says so -- a test that passes only when the PDF is malformed.
    """
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def test_it_is_a_pdf():
    pdf = render_invoice(_view())

    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_it_names_the_order_and_the_shop():
    """Searchable in a downloads folder six months later."""
    pdf = render_invoice(_view())

    assert "ord_abc123" in text_of(pdf)
    assert "Campus Print" in text_of(pdf)


def test_a_refunded_order_says_so():
    """A receipt that still reads as money taken is the one that gets waved at
    a shop counter."""
    refunded = _view(refunded_at=datetime(2026, 8, 23, tzinfo=UTC))

    assert "REFUNDED" in text_of(render_invoice(refunded))
    assert "REFUNDED" not in text_of(render_invoice(_view()))


def test_the_fee_line_appears_only_when_a_fee_was_charged():
    """Wallet payments carry none, and a zero line invites the question."""
    assert "Payment fee" not in text_of(render_invoice(_view()))
    assert "Payment fee" in text_of(
        render_invoice(_view(fee_inr=Decimal("2.00"), total_inr=Decimal("17.00")))
    )


def test_many_documents_all_appear():
    many = _view(items=[_line(filename=f"file-{n}.pdf") for n in range(12)])

    text = text_of(render_invoice(many))

    assert "file-0.pdf" in text and "file-11.pdf" in text


def test_a_very_long_filename_cannot_run_into_the_amount():
    text = text_of(render_invoice(_view(items=[_line(filename="x" * 300 + ".pdf")])))

    assert "x" * 300 not in text
    assert "…" in text


def test_a_deleted_document_still_has_a_line():
    """Retention removes the file and keeps the row, so a year-old receipt
    still says what was bought."""
    text = text_of(render_invoice(_view(items=[_line(filename=None, document_id=None)])))

    assert "Deleted file" in text
