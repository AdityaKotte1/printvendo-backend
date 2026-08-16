"""What an order costs, decided once.

The old backend priced a job in `jobs.py` and added the platform fee in
`payments.py`, so the number a student was shown and the number they were
charged were assembled by different code. Here one function produces both.
"""

from decimal import Decimal

import pytest

from app.modules.orders.quotes import (
    FEE_CAP_INR,
    FEE_RATE,
    LineQuote,
    OrderQuote,
    gateway_fee,
    quote_line,
)
from app.modules.printing import PrintOptions


def options(**kwargs) -> PrintOptions:
    defaults = {"total_pages": 10, "colour": False, "duplex": False, "copies": 1}
    return PrintOptions.create(**{**defaults, **kwargs})


PRICES = {
    "bw_single": Decimal("2.00"),
    "bw_double": Decimal("1.50"),
    "color_single": Decimal("10.00"),
    "color_double": Decimal("8.00"),
}


# ── one document ────────────────────────────────────────────────────────────


def test_black_and_white_single_sided_is_priced_per_impression():
    line = quote_line(options(), total_pages=10, prices=PRICES)

    assert line.impressions == 10
    assert line.amount_inr == Decimal("20.00")


def test_duplex_uses_the_duplex_rate_and_still_counts_sides():
    """Duplex is cheaper *per side*, and a ten-page duplex job still prints ten
    sides. Charging by sheets here would halve the revenue on every duplex job."""
    line = quote_line(options(duplex=True), total_pages=10, prices=PRICES)

    assert line.impressions == 10
    assert line.sheets == 5
    assert line.amount_inr == Decimal("15.00")


def test_colour_uses_the_colour_rate():
    line = quote_line(options(colour=True), total_pages=10, prices=PRICES)

    assert line.amount_inr == Decimal("100.00")


def test_colour_duplex_uses_the_colour_duplex_rate():
    line = quote_line(
        options(colour=True, duplex=True), total_pages=10, prices=PRICES
    )

    assert line.amount_inr == Decimal("80.00")


def test_copies_multiply_the_price():
    line = quote_line(options(copies=3), total_pages=10, prices=PRICES)

    assert line.impressions == 30
    assert line.amount_inr == Decimal("60.00")


def test_a_page_range_is_charged_on_the_selection_not_the_document():
    line = quote_line(
        options(page_range="1,4-6"), total_pages=10, prices=PRICES
    )

    assert line.impressions == 4
    assert line.amount_inr == Decimal("8.00")


def test_the_price_comes_from_the_kiosk_being_printed_at():
    """Two kiosks, same document, different money. The old backend's client-side
    estimate had its own copy of the rates."""
    dearer = {**PRICES, "bw_single": Decimal("5.00")}

    assert quote_line(options(), total_pages=10, prices=dearer).amount_inr == Decimal(
        "50.00"
    )


def test_a_float_price_is_refused_rather_than_rounded():
    """A float has already lost precision by the time it arrives."""
    with pytest.raises(TypeError):
        quote_line(options(), total_pages=10, prices={**PRICES, "bw_single": 2.0})


# ── the fee (D9) ────────────────────────────────────────────────────────────


def test_the_fee_is_two_percent_of_a_small_order():
    assert gateway_fee(Decimal("50.00")) == Decimal("1.00")


def test_the_fee_is_capped_in_rupees():
    """min(2%, ₹2). A hundred-rupee order is where the cap starts binding."""
    assert gateway_fee(Decimal("100.00")) == Decimal("2.00")
    assert gateway_fee(Decimal("5000.00")) == Decimal("2.00")


def test_the_fee_rounds_half_up_to_paise():
    assert gateway_fee(Decimal("12.55")) == Decimal("0.25")


def test_the_fee_on_nothing_is_nothing():
    assert gateway_fee(Decimal("0.00")) == Decimal("0.00")


def test_the_fee_constants_match_the_recorded_decision():
    """D9 is a commercial decision, not an implementation detail. If someone
    changes the rate they should have to change a test that says so."""
    assert FEE_RATE == Decimal("0.02")
    assert FEE_CAP_INR == Decimal("2.00")


# ── the whole order ─────────────────────────────────────────────────────────


def _line(amount: str) -> LineQuote:
    return LineQuote(
        impressions=1, sheets=1, amount_inr=Decimal(amount), page_count=1
    )


def test_an_order_totals_its_lines_plus_the_gateway_fee():
    quote = OrderQuote.build(
        [_line("20.00"), _line("30.00")], pays_by_wallet=False
    )

    assert quote.subtotal_inr == Decimal("50.00")
    assert quote.fee_inr == Decimal("1.00")
    assert quote.total_inr == Decimal("51.00")


def test_a_wallet_order_carries_no_fee():
    """D8: the wallet is platform-kiosk-only and fee-free. Charging one here
    would take a second cut of money the platform already holds."""
    quote = OrderQuote.build([_line("50.00")], pays_by_wallet=True)

    assert quote.fee_inr == Decimal("0.00")
    assert quote.total_inr == Decimal("50.00")


def test_an_order_of_many_documents_sums_exactly():
    """Quantized once at the end, so thirty lines of 33 paise do not drift."""
    lines = [_line("0.33")] * 30
    quote = OrderQuote.build(lines, pays_by_wallet=True)

    assert quote.subtotal_inr == Decimal("9.90")


def test_a_sub_paise_line_is_rounded_once_at_the_end_not_per_line():
    """Rounding each line first and adding afterwards gives a different answer,
    and it is the answer that is wrong: three half-paise lines are 1.5 paise,
    which is 2 paise, not 3. Nothing produces sub-paise lines yet -- a discount
    or a percentage-based shop item will -- so the property is pinned now, while
    it is cheap, rather than discovered as a one-paise discrepancy in a ledger.
    """
    quote = OrderQuote.build([_line("0.005")] * 3, pays_by_wallet=True)

    assert quote.subtotal_inr == Decimal("0.02")


def test_an_empty_order_is_refused():
    from app.core.errors import BadRequest

    with pytest.raises(BadRequest):
        OrderQuote.build([], pays_by_wallet=True)
