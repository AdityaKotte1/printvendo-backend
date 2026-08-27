"""The look of a piece of paper Printvendo produces.

Two documents are printed by this system and both are money: a student's
receipt for an order, and an owner's invoice for a subscription. They are made
in different bounded contexts -- `orders` and `billing` -- which may not import
each other, so without a home down here the brand would exist twice and drift.
A shop would then hold two documents from the same company that did not look
like the same company.

**Chrome only.** Nothing here knows what a receipt or an invoice *is*. It draws
a band, a rule and a row, and the module that owns the numbers decides what
goes in them. The moment this file grows a `total` it has become a second
opinion about money.

The palette is the app's ink, from `printvendo-web/styles/tokens.css`, copied
rather than imported: a shared constants file between a Python service and a
CSS bundle is a promise neither side can keep. Copied **once**, though, which
is the difference between this and what it replaced.
"""

from decimal import Decimal

from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

TONER = Color(0.06, 0.06, 0.06)
ORANGE = Color(1.0, 0.42, 0.28)
PAPER = Color(1, 1, 1)
FAINT = Color(0.45, 0.45, 0.45)

PAGE_SIZE = A4
MARGIN = 18 * mm
BAND_HEIGHT = 26 * mm

WORDMARK = "PRINTVENDO"
SUPPORT = "support@printvendo.com"


def money(value: Decimal) -> str:
    """Rupees, grouped, two places.

    `Rs` rather than the rupee sign: the sign is not in reportlab's built-in
    Helvetica, and a document that renders it as a black box is worse than one
    that spells it. Grouping is Western rather than Indian for the same reason
    every other number in this system is -- one format, everywhere, so two
    figures can be compared without deciding which convention each is in.
    """
    return f"Rs {value:,.2f}"


def band(pdf, *, label: str) -> float:
    """The black band, the wordmark, and the orange rule under it.

    Returns the y coordinate to start writing at, so no caller has to know how
    tall the band is -- which is the number that would otherwise be copied into
    each document and be wrong in one of them after a change here.
    """
    width, height = PAGE_SIZE

    pdf.setFillColor(TONER)
    pdf.rect(0, height - BAND_HEIGHT, width, BAND_HEIGHT, stroke=0, fill=1)

    pdf.setFillColor(PAPER)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(MARGIN, height - BAND_HEIGHT + 9 * mm, WORDMARK)

    pdf.setFillColor(ORANGE)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawRightString(width - MARGIN, height - BAND_HEIGHT + 10 * mm, label)

    pdf.rect(0, height - BAND_HEIGHT - 2 * mm, width, 2 * mm, stroke=0, fill=1)

    return height - BAND_HEIGHT - 16 * mm


def rule(pdf, y: float, *, heavy: bool = False) -> None:
    """A horizontal line the width of the text block."""
    width, _ = PAGE_SIZE
    pdf.setStrokeColor(TONER if heavy else FAINT)
    pdf.setLineWidth(1 if heavy else 0.4)
    pdf.line(MARGIN, y, width - MARGIN, y)


def foot(pdf, *, note: str) -> None:
    """The small print at the bottom of the page.

    Deliberately carries no "generated on" date. A document that says when it
    was printed is a document that differs from the copy somebody printed last
    week, and the whole value of one is that two people are looking at the
    same thing.
    """
    width, _ = PAGE_SIZE
    pdf.setFillColor(FAINT)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawString(MARGIN, MARGIN + 5 * mm, note)
    pdf.drawString(MARGIN, MARGIN, f"Questions: {SUPPORT}")
    pdf.drawRightString(width - MARGIN, MARGIN, "printvendo.com")
