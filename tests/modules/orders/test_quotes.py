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


# The units matter and used to be got wrong: a `_single` price is **one sheet
# printed on one side**, and a `_double` price is **one sheet printed on both**.
# So double is dearer than single per *sheet* -- same paper, twice the toner --
# and cheaper per *page*, which is the whole reason a student picks it. These
# are also the platform defaults, and `set_pricing` refuses double below single;
# the old figures here (1.50 against 2.00) were a combination the product would
# not have stored.
PRICES = {
    "bw_single": Decimal("2.00"),
    "bw_double": Decimal("3.00"),
    "color_single": Decimal("10.00"),
    "color_double": Decimal("18.00"),
}


# ── one document ────────────────────────────────────────────────────────────


def test_black_and_white_single_sided_is_priced_per_impression():
    line = quote_line(options(), total_pages=10, prices=PRICES)

    assert line.impressions == 10
    assert line.amount_inr == Decimal("20.00")


def test_duplex_is_charged_by_the_sheet_it_actually_uses():
    """Ten pages, five sheets, five double-sided sheets at the double rate.

    This used to multiply the double rate by the number of *sides*, so a duplex
    job cost 10 x 3.00 = 30.00 -- half the paper for one and a half times the
    money. A student reading "double-sided" and being charged more for it is
    right to think something is wrong.
    """
    line = quote_line(options(duplex=True), total_pages=10, prices=PRICES)

    assert line.impressions == 10
    assert line.sheets == 5
    assert line.amount_inr == Decimal("15.00")


def test_the_odd_page_of_a_duplex_job_is_charged_as_a_single_side():
    """Seven pages is three sheets printed both sides and one printed on one.

    That last sheet is a single-sided sheet however the job was submitted, and
    charging the double rate for it would be charging for a side that is blank.
    """
    line = quote_line(options(duplex=True), total_pages=7, prices=PRICES)

    assert line.sheets == 4
    assert line.amount_inr == Decimal("11.00")  # 3 x 3.00 + 1 x 2.00


def test_a_one_page_duplex_job_costs_a_single_sided_sheet():
    line = quote_line(options(duplex=True), total_pages=1, prices=PRICES)

    assert line.amount_inr == Decimal("2.00")


def test_each_copy_rounds_its_own_odd_page():
    """Two copies of seven pages is eight sheets, not seven and a half.

    Copies do not share a sheet -- the back of the last page of copy one is not
    the front of copy two -- so the odd page is charged twice.
    """
    line = quote_line(options(duplex=True, copies=2), total_pages=7, prices=PRICES)

    assert line.sheets == 8
    assert line.amount_inr == Decimal("22.00")  # 2 x (3 x 3.00 + 1 x 2.00)


@pytest.mark.parametrize("pages", [1, 2, 3, 7, 10, 11, 50])
@pytest.mark.parametrize("colour", [False, True])
def test_duplex_never_costs_more_than_the_same_job_single_sided(pages, colour):
    """The property the defect broke, stated so it cannot break again quietly.

    Duplex uses half the paper. Whatever the rates, it must not cost more --
    and `set_pricing` refusing a double rate below the single one is what keeps
    that true for any prices an owner can actually set.
    """
    duplex = quote_line(
        options(duplex=True, colour=colour), total_pages=pages, prices=PRICES
    )
    simplex = quote_line(
        options(duplex=False, colour=colour), total_pages=pages, prices=PRICES
    )

    assert duplex.amount_inr <= simplex.amount_inr


def test_colour_uses_the_colour_rate():
    line = quote_line(options(colour=True), total_pages=10, prices=PRICES)

    assert line.amount_inr == Decimal("100.00")


def test_colour_duplex_uses_the_colour_duplex_rate():
    line = quote_line(
        options(colour=True, duplex=True), total_pages=10, prices=PRICES
    )

    assert line.amount_inr == Decimal("90.00")  # 5 sheets x 18.00


def test_copies_multiply_the_price():
    line = quote_line(options(copies=3), total_pages=10, prices=PRICES)

    assert line.impressions == 30
    assert line.amount_inr == Decimal("60.00")


def test_single_sided_is_one_sheet_per_page():
    """No rounding to do: every page is its own sheet, printed on one side."""
    line = quote_line(options(), total_pages=7, prices=PRICES)

    assert line.sheets == 7
    assert line.amount_inr == Decimal("14.00")


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


def test_colour_costs_the_same_per_page_either_way_at_the_default_rates():
    """The operator's rule: colour is 10.00 a side, duplex or not.

    It falls out of the same sheet-priced model rather than needing a special
    case -- a colour sheet printed on both sides is priced at two sides' worth.
    Black and white is where the duplex discount lives.
    """
    from app.modules.kiosks.pricing import DEFAULT_PRICES

    simplex = quote_line(options(), total_pages=10, prices=DEFAULT_PRICES)
    duplex = quote_line(options(duplex=True), total_pages=10, prices=DEFAULT_PRICES)
    colour_simplex = quote_line(options(colour=True), total_pages=10, prices=DEFAULT_PRICES)
    colour_duplex = quote_line(
        options(colour=True, duplex=True), total_pages=10, prices=DEFAULT_PRICES
    )

    assert colour_simplex.amount_inr == Decimal("100.00")
    assert colour_duplex.amount_inr == Decimal("100.00")
    # And black and white is genuinely cheaper double-sided.
    assert simplex.amount_inr == Decimal("20.00")
    assert duplex.amount_inr == Decimal("15.00")
