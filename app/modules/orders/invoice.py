"""A receipt for an order, as a PDF.

**Bytes from an authenticated route, never a URL.** The old admin dashboard
built a link and handed it to the browser, which is how a proof that failed to
load looked exactly like one nobody had uploaded. The same rule holds here for
the same reason: what a student downloads is produced by a request that has
already proved who they are.

The design follows the app rather than a generic invoice template -- a black
band, an orange rule, hard edges and no rounded corners -- so a receipt read on
a phone is recognisably the same product as the screen it came from. There is
deliberately no logo file to keep in step: the wordmark is set in type.

Only a paid order has a receipt. An unpaid one is a quote, and printing "TOTAL"
against money nobody has taken is how a document ends up being waved at a shop
as proof of a payment that never happened.
"""

import io
from datetime import UTC, datetime
from decimal import Decimal

from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from app.modules.orders.views import OrderView

# The app's ink, from printvendo-web/styles/tokens.css. Kept as literals rather
# than imported from anywhere: this is the one place they are needed, and a
# shared constants file between a Python service and a CSS bundle would be a
# promise neither side can keep.
TONER = Color(0.06, 0.06, 0.06)
ORANGE = Color(1.0, 0.42, 0.28)
PAPER = Color(1, 1, 1)
FAINT = Color(0.45, 0.45, 0.45)

MARGIN = 18 * mm
BAND_HEIGHT = 26 * mm

SUPPORT = "support@printvendo.com"


def _money(value: Decimal) -> str:
    return f"Rs {value:,.2f}"


def _describe(item) -> str:
    """One line saying how a document was printed, in words a person reads."""
    parts = [
        "Colour" if item.colour else "Black & white",
        "both sides" if item.duplex else "one side",
        f"{item.page_count} page{'s' if item.page_count != 1 else ''}",
    ]
    if item.copies > 1:
        parts.append(f"{item.copies} copies")
    if item.page_range:
        parts.append(f"pages {item.page_range}")
    return " · ".join(parts)


def render_invoice(view: OrderView, *, student_email: str | None = None) -> bytes:
    """The receipt for one paid order.

    Takes the view rather than the ORM row, so this cannot reach for anything
    the caller has not already been allowed to see -- and so the same function
    serves a student, and later an owner, without either getting the other's
    fields.
    """
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    pdf.setTitle(f"Receipt {view.id}")
    pdf.setAuthor("Printvendo")
    pdf.setSubject(f"Receipt for order {view.id}")

    # ── the band ────────────────────────────────────────────────────────────
    pdf.setFillColor(TONER)
    pdf.rect(0, height - BAND_HEIGHT, width, BAND_HEIGHT, stroke=0, fill=1)

    pdf.setFillColor(PAPER)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(MARGIN, height - BAND_HEIGHT + 9 * mm, "PRINTVENDO")

    pdf.setFillColor(ORANGE)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawRightString(width - MARGIN, height - BAND_HEIGHT + 10 * mm, "RECEIPT")

    # The orange rule under the band, which the app uses as its accent.
    pdf.setFillColor(ORANGE)
    pdf.rect(0, height - BAND_HEIGHT - 2 * mm, width, 2 * mm, stroke=0, fill=1)

    y = height - BAND_HEIGHT - 16 * mm

    # ── who, what, when ─────────────────────────────────────────────────────
    pdf.setFillColor(TONER)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(MARGIN, y, view.kiosk_name or "Printvendo")

    y -= 7 * mm
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(FAINT)
    paid = view.paid_at or view.created_at
    pdf.drawString(MARGIN, y, f"Order {view.id}")
    pdf.drawRightString(
        width - MARGIN, y, paid.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")
    )

    if student_email:
        y -= 5 * mm
        pdf.drawString(MARGIN, y, student_email)

    if view.payment_method:
        method = "Wallet balance" if view.payment_method == "wallet" else "Card / UPI"
        pdf.drawRightString(width - MARGIN, y, f"Paid by {method}")

    # ── the lines ───────────────────────────────────────────────────────────
    y -= 12 * mm
    pdf.setStrokeColor(TONER)
    pdf.setLineWidth(1)
    pdf.line(MARGIN, y, width - MARGIN, y)

    y -= 6 * mm
    pdf.setFont("Helvetica-Bold", 8)
    pdf.setFillColor(FAINT)
    pdf.drawString(MARGIN, y, "WHAT WAS PRINTED")
    pdf.drawRightString(width - MARGIN - 26 * mm, y, "SHEETS")
    pdf.drawRightString(width - MARGIN, y, "AMOUNT")

    y -= 3 * mm
    pdf.setStrokeColor(FAINT)
    pdf.setLineWidth(0.4)
    pdf.line(MARGIN, y, width - MARGIN, y)

    for item in view.items:
        y -= 8 * mm
        pdf.setFillColor(TONER)
        pdf.setFont("Helvetica-Bold", 10)
        # A filename can be long and is somebody's own text; it is trimmed
        # rather than allowed to run into the amount column.
        name = item.filename or "Deleted file"
        pdf.drawString(MARGIN, y, name[:52] + ("…" if len(name) > 52 else ""))
        pdf.drawRightString(width - MARGIN - 26 * mm, y, str(item.sheets))
        pdf.drawRightString(width - MARGIN, y, _money(item.amount_inr))

        y -= 4.5 * mm
        pdf.setFont("Helvetica", 8)
        pdf.setFillColor(FAINT)
        pdf.drawString(MARGIN, y, _describe(item))

        if y < 60 * mm:
            pdf.showPage()
            y = height - MARGIN

    # ── the money ───────────────────────────────────────────────────────────
    y -= 8 * mm
    pdf.setStrokeColor(TONER)
    pdf.setLineWidth(1)
    pdf.line(width / 2, y, width - MARGIN, y)

    def total_row(label: str, value: str, *, bold: bool = False, size: int = 10) -> None:
        nonlocal y
        y -= 6 * mm
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        pdf.setFillColor(TONER if bold else FAINT)
        pdf.drawString(width / 2, y, label)
        pdf.setFillColor(TONER)
        pdf.drawRightString(width - MARGIN, y, value)

    total_row("Printing", _money(view.subtotal_inr))
    if view.fee_inr > 0:
        total_row("Payment fee", _money(view.fee_inr))
    total_row("Total paid", _money(view.total_inr), bold=True, size=12)

    if view.refunded_at is not None:
        y -= 8 * mm
        pdf.setFillColor(ORANGE)
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(
            MARGIN,
            y,
            "REFUNDED "
            + view.refunded_at.astimezone(UTC).strftime("%d %b %Y"),
        )

    # ── the foot ────────────────────────────────────────────────────────────
    pdf.setFillColor(FAINT)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawString(
        MARGIN,
        MARGIN + 5 * mm,
        "Computer-generated receipt. No signature is required.",
    )
    pdf.drawString(MARGIN, MARGIN, f"Questions about this order: {SUPPORT}")
    pdf.drawRightString(
        width - MARGIN,
        MARGIN,
        f"Generated {datetime.now(UTC).strftime('%d %b %Y')}",
    )

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
