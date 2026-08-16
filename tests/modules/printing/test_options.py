import pytest

from app.core.errors import BadRequest
from app.modules.printing.options import (
    MAX_COPIES,
    PrintOptions,
    format_page_range,
    parse_page_range,
    workload,
)

# ── page range parsing ──────────────────────────────────────────────────────


def test_no_range_means_every_page():
    assert parse_page_range(None, total_pages=5) == [1, 2, 3, 4, 5]


def test_an_empty_range_means_every_page():
    assert parse_page_range("   ", total_pages=3) == [1, 2, 3]


def test_a_single_page():
    assert parse_page_range("3", total_pages=5) == [3]


def test_a_simple_range():
    assert parse_page_range("2-4", total_pages=5) == [2, 3, 4]


def test_ranges_are_sorted():
    """CUPS requires ascending order, so "12-17,1" must not reach it as typed."""
    assert parse_page_range("4-5,1", total_pages=10) == [1, 4, 5]


def test_overlapping_ranges_are_deduplicated():
    """Otherwise the student is charged twice for a page printed once."""
    assert parse_page_range("1-3,2-4", total_pages=10) == [1, 2, 3, 4]


def test_whitespace_is_tolerated():
    assert parse_page_range(" 1 , 3 - 4 ", total_pages=5) == [1, 3, 4]


def test_a_page_beyond_the_document_is_refused(_=None):
    """Refused, not clamped: printing pages 5-10 while charging for 5-40 is
    worse than saying the request is wrong."""
    with pytest.raises(BadRequest) as caught:
        parse_page_range("5-40", total_pages=10)
    assert "10 pages" in str(caught.value)


def test_page_zero_is_refused():
    with pytest.raises(BadRequest):
        parse_page_range("0-3", total_pages=10)


def test_a_backwards_range_is_refused_with_a_useful_message():
    with pytest.raises(BadRequest) as caught:
        parse_page_range("7-3", total_pages=10)
    assert "3-7" in str(caught.value)


def test_nonsense_is_refused():
    for bad in ("abc", "1-", "-3", "1..3", "1;2", "one"):
        with pytest.raises(BadRequest):
            parse_page_range(bad, total_pages=10)


def test_a_range_of_only_commas_is_refused():
    with pytest.raises(BadRequest):
        parse_page_range(",,,", total_pages=10)


# ── formatting back ─────────────────────────────────────────────────────────


def test_consecutive_pages_collapse_to_a_range():
    assert format_page_range([1, 2, 3]) == "1-3"


def test_singles_and_ranges_mix():
    assert format_page_range([1, 4, 5, 6, 9]) == "1,4-6,9"


def test_a_single_page_formats_alone():
    assert format_page_range([7]) == "7"


def test_a_pair_is_a_range_not_two_singles():
    assert format_page_range([3, 4]) == "3-4"


def test_parse_and_format_round_trip():
    pages = parse_page_range("12-17,1", total_pages=20)
    assert format_page_range(pages) == "1,12-17"


# ── building options ────────────────────────────────────────────────────────


def test_defaults_are_mono_single_sided_one_copy():
    o = PrintOptions.create(total_pages=10)
    assert (o.colour, o.duplex, o.copies, o.page_range) == (False, False, 1, None)


def test_a_full_range_is_stored_as_none():
    """"All pages" gets one representation, not two that must be kept in step."""
    o = PrintOptions.create(total_pages=5, page_range="1-5")
    assert o.page_range is None


def test_a_partial_range_is_stored_normalised():
    o = PrintOptions.create(total_pages=20, page_range="12-17,1")
    assert o.page_range == "1,12-17"


def test_zero_copies_is_refused():
    with pytest.raises(BadRequest):
        PrintOptions.create(total_pages=5, copies=0)


def test_negative_copies_is_refused():
    with pytest.raises(BadRequest):
        PrintOptions.create(total_pages=5, copies=-3)


def test_an_absurd_number_of_copies_is_refused(_=None):
    """A kiosk tray does not hold a thousand copies, and a typo should not
    empty it."""
    with pytest.raises(BadRequest):
        PrintOptions.create(total_pages=5, copies=MAX_COPIES + 1)


def test_options_are_frozen():
    """Nothing may adjust what an order was priced against."""
    import dataclasses

    o = PrintOptions.create(total_pages=5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        o.copies = 9


# ── the one calculation ─────────────────────────────────────────────────────


def test_simple_single_sided():
    o = PrintOptions.create(total_pages=10)
    w = workload(o, total_pages=10)
    assert (w.pages, w.selected_pages, w.impressions, w.sheets) == (10, 10, 10, 10)


def test_copies_multiply_impressions_and_sheets():
    o = PrintOptions.create(total_pages=10, copies=3)
    w = workload(o, total_pages=10)
    assert w.impressions == 30
    assert w.sheets == 30


def test_duplex_halves_the_paper_not_the_price():
    """10 pages, 2 copies, double-sided: 20 impressions, 10 sheets. Charging for
    sheets or deducting paper by impressions is how a shop misreads its books."""
    o = PrintOptions.create(total_pages=10, duplex=True, copies=2)
    w = workload(o, total_pages=10)
    assert w.impressions == 20
    assert w.sheets == 10


def test_duplex_rounds_up_for_an_odd_page_count():
    """5 sides needs 3 sheets -- the last one printed on one side only."""
    o = PrintOptions.create(total_pages=5, duplex=True)
    w = workload(o, total_pages=5)
    assert w.impressions == 5
    assert w.sheets == 3


def test_each_copy_rounds_separately(_=None):
    """Two copies of a 5-page duplex document is 6 sheets, not 5.

    Copies do not share a sheet: the last page of copy one and the first page of
    copy two must not end up on the same piece of paper.
    """
    o = PrintOptions.create(total_pages=5, duplex=True, copies=2)
    w = workload(o, total_pages=5)
    assert w.sheets == 6


def test_a_page_range_reduces_the_workload():
    o = PrintOptions.create(total_pages=100, page_range="1-10")
    w = workload(o, total_pages=100)
    assert w.selected_pages == 10
    assert w.impressions == 10
    assert w.pages == 100


def test_range_copies_and_duplex_together():
    """7 selected pages, 3 copies, duplex: 21 impressions, 4 sheets per copy,
    12 sheets total."""
    o = PrintOptions.create(total_pages=50, page_range="1-7", duplex=True, copies=3)
    w = workload(o, total_pages=50)
    assert w.selected_pages == 7
    assert w.impressions == 21
    assert w.sheets == 12


def test_a_single_page_duplex_still_uses_one_sheet():
    o = PrintOptions.create(total_pages=1, duplex=True)
    assert workload(o, total_pages=1).sheets == 1


def test_colour_does_not_change_the_workload():
    """Colour changes the price, never the amount of paper."""
    mono = workload(PrintOptions.create(total_pages=10), total_pages=10)
    colour = workload(
        PrintOptions.create(total_pages=10, colour=True), total_pages=10
    )
    assert mono.sheets == colour.sheets
    assert mono.impressions == colour.impressions
