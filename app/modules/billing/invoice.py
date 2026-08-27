"""What an owner paid Printvendo, as a document they can file.

A shop that pays for software needs paper for it. The legacy owner app had a
printable invoice and the rewire dropped it, because the endpoint behind it did
not survive; this is the replacement, built where the numbers already are
rather than assembled in a browser out of four fields.

**Bytes from an authenticated route, never a URL.** The same rule as the
student receipt and the account-ownership proof, for the same reason: the old
admin dashboard built links, and a document that failed to load looked exactly
like one that was never there.

**It exists only for money that arrived.** A subscription still waiting to be
paid for is a quote, and a granted trial cost nothing at all. Printing "TOTAL
PAID" against either is how a document ends up being waved at somebody as proof
of a payment that never happened.

**It is the same document every time.** The number is derived from the
subscription and the date is read off the capture, so downloading it twice
produces one invoice rather than two. A counter would have to survive a
rollback and be unique across the estate; the subscription's public id already
is both, and a number you can look the subscription up by is worth more than a
number that counts.

There is deliberately **no GSTIN and no tax breakdown**. Printvendo does not
hold a tax registration in this system -- nothing stores one, for either side --
and a document that prints a tax line it cannot substantiate is worse than one
that does not claim to. `InvoiceParty.lines` is where those details go when
there are any: the renderer prints what it is handed.
"""

import io
from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from app.core.documents import (
    FAINT,
    MARGIN,
    ORANGE,
    PAGE_SIZE,
    TONER,
    band,
    foot,
    money,
    rule,
)
from app.core.errors import Conflict
from app.modules.billing.models import Subscription

NOT_PAID_FOR = (
    "That subscription has not been paid for, so there is no invoice for it yet."
)

INVOICE_PREFIX = "PV"


class InvoiceParty(NamedTuple):
    """One side of the document.

    `lines` is free text -- a trading name, a street address, a registration
    number if there ever is one. The renderer prints what it is given and knows
    nothing about what any line means, so adding a field to an owner's billing
    details later is a change in one place rather than here as well.
    """

    name: str
    email: str | None = None
    lines: tuple[str, ...] = ()


def invoice_number(subscription: Subscription) -> str:
    """Stable, unique, and something you can look the subscription up by."""
    return f"{INVOICE_PREFIX}-{subscription.public_id.upper()}"


def render_subscription_invoice(
    subscription: Subscription,
    *,
    plan_name: str,
    billed_to: InvoiceParty,
    billed_by: InvoiceParty,
    paid_at: datetime | None,
    payment_reference: str | None,
) -> bytes:
    """The invoice for one paid subscription.

    Takes the parties as arguments rather than reading a `User`: billing does
    not own accounts, and handing it the two names keeps the document a
    rendering job. It also means the same function serves an owner downloading
    their own and an admin producing a copy, with neither getting a field the
    other would not have.
    """
    if paid_at is None or subscription.total_amount <= Decimal("0.00"):
        raise Conflict(NOT_PAID_FOR)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=PAGE_SIZE)
    width, _ = PAGE_SIZE

    number = invoice_number(subscription)
    pdf.setTitle(f"Invoice {number}")
    pdf.setAuthor(billed_by.name)
    pdf.setSubject(f"Printvendo subscription {subscription.public_id}")

    y = band(pdf, label="INVOICE")

    # ── the number and the date ─────────────────────────────────────────────
    pdf.setFillColor(TONER)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(MARGIN, y, number)

    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(FAINT)
    pdf.drawRightString(
        width - MARGIN, y, paid_at.astimezone(UTC).strftime("%d %b %Y")
    )

    # ── who to whom ─────────────────────────────────────────────────────────
    y -= 14 * mm
    left, right = MARGIN, width / 2

    def party(party: InvoiceParty, x: float, heading: str, top: float) -> float:
        pdf.setFont("Helvetica-Bold", 8)
        pdf.setFillColor(FAINT)
        pdf.drawString(x, top, heading)

        row = top - 6 * mm
        pdf.setFont("Helvetica-Bold", 11)
        pdf.setFillColor(TONER)
        pdf.drawString(x, row, party.name)

        pdf.setFont("Helvetica", 9)
        pdf.setFillColor(FAINT)
        for line in (*party.lines, *( (party.email,) if party.email else () )):
            row -= 4.6 * mm
            pdf.drawString(x, row, line)
        return row

    bottom = min(
        party(billed_by, left, "FROM", y),
        party(billed_to, right, "BILLED TO", y),
    )

    # ── what was bought ─────────────────────────────────────────────────────
    y = bottom - 14 * mm
    rule(pdf, y, heavy=True)

    y -= 6 * mm
    pdf.setFont("Helvetica-Bold", 8)
    pdf.setFillColor(FAINT)
    pdf.drawString(MARGIN, y, "WHAT THIS PAYS FOR")
    pdf.drawRightString(width - MARGIN - 30 * mm, y, "A MONTH")
    pdf.drawRightString(width - MARGIN, y, "AMOUNT")

    y -= 3 * mm
    rule(pdf, y)

    months = subscription.duration_months
    # What the term costs before any discount. Shown so the arithmetic on the
    # page works: without it the document says six months at a thousand rupees
    # and then a smaller total, with nothing accounting for the gap.
    list_total = subscription.monthly_price_charged * months

    y -= 8 * mm
    pdf.setFillColor(TONER)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(MARGIN, y, f"{plan_name} subscription")
    pdf.drawRightString(width - MARGIN - 30 * mm, y, money(subscription.monthly_price_charged))
    pdf.drawRightString(width - MARGIN, y, money(list_total))

    y -= 4.5 * mm
    pdf.setFont("Helvetica", 8)
    pdf.setFillColor(FAINT)
    pdf.drawString(MARGIN, y, f"{months} month{'s' if months != 1 else ''}")

    if subscription.starts_at and subscription.expires_at:
        pdf.drawRightString(
            width - MARGIN,
            y,
            subscription.starts_at.astimezone(UTC).strftime("%d %b %Y")
            + " to "
            + subscription.expires_at.astimezone(UTC).strftime("%d %b %Y"),
        )

    # ── the money ───────────────────────────────────────────────────────────
    y -= 10 * mm
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

    total_row("Subtotal", money(list_total))
    if subscription.discount_percent > Decimal("0.00"):
        total_row(
            f"Discount {subscription.discount_percent:.0f}%",
            "-" + money(list_total - subscription.total_amount),
        )
    total_row("Total paid", money(subscription.total_amount), bold=True, size=12)

    # ── how it was paid ─────────────────────────────────────────────────────
    if payment_reference:
        y -= 10 * mm
        pdf.setFillColor(FAINT)
        pdf.setFont("Helvetica", 8)
        # The line that matches this document to a line on a bank statement.
        pdf.drawString(MARGIN, y, f"Payment reference {payment_reference}")

    if subscription.free_until is not None:
        y -= 5 * mm
        pdf.setFillColor(ORANGE)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(
            MARGIN,
            y,
            "FREE UNTIL "
            + subscription.free_until.astimezone(UTC).strftime("%d %b %Y").upper(),
        )

    foot(pdf, note="Computer-generated invoice. No signature is required.")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
