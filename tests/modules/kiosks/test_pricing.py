from decimal import Decimal

import pytest

from app.core.errors import BadRequest
from app.modules.kiosks.models import Kiosk
from app.modules.kiosks.pricing import (
    DEFAULT_PRICES,
    UNBOUNDED,
    PlatformBand,
    PriceBand,
    effective_prices,
    read_pricing,
    set_pricing,
)
from app.modules.kiosks.registry import create_kiosk


class FixedBand:
    def __init__(self, band: PriceBand) -> None:
        self.band = band

    def band_for(self, db, kiosk) -> PriceBand:
        return self.band


OPEN = FixedBand(UNBOUNDED)
NARROW = FixedBand(
    PriceBand(
        floor_bw=Decimal("1.50"),
        ceiling_bw=Decimal("5.00"),
        floor_color=Decimal("8.00"),
        ceiling_color=Decimal("20.00"),
    )
)


@pytest.fixture
def kiosk(db_session) -> Kiosk:
    k = create_kiosk(db_session, name="Library")
    db_session.flush()
    return k


def test_a_kiosk_with_no_prices_uses_the_defaults(db_session, kiosk):
    assert effective_prices(kiosk) == DEFAULT_PRICES


def test_setting_a_price_overrides_the_default(db_session, kiosk):
    set_pricing(db_session, kiosk, bands=OPEN, bw_single=Decimal("3"))
    assert effective_prices(kiosk)["bw_single"] == Decimal("3.00")


def test_reading_returns_prices_and_band_together(db_session, kiosk):
    """A client that fetches limits separately will eventually show a stale
    one."""
    payload = read_pricing(db_session, kiosk, bands=NARROW)
    assert set(payload) == {"prices", "band"}
    assert payload["band"]["floor_bw"] == Decimal("1.50")


def test_a_price_below_the_floor_is_refused(db_session, kiosk):
    with pytest.raises(BadRequest) as caught:
        set_pricing(db_session, kiosk, bands=NARROW, bw_single=Decimal("1.00"))
    assert "less than" in str(caught.value)


def test_a_price_above_the_ceiling_is_refused(db_session, kiosk):
    with pytest.raises(BadRequest) as caught:
        set_pricing(db_session, kiosk, bands=NARROW, bw_single=Decimal("9.00"))
    assert "more than" in str(caught.value)


def test_colour_uses_the_colour_band_not_the_mono_one(db_session, kiosk):
    """8.00 is fine for colour and far above the mono ceiling; picking the wrong
    band would reject a legitimate price."""
    set_pricing(
        db_session,
        kiosk,
        bands=NARROW,
        color_single=Decimal("8.00"),
        color_double=Decimal("14.00"),
    )
    assert effective_prices(kiosk)["color_single"] == Decimal("8.00")


def test_a_price_at_the_floor_is_allowed(db_session, kiosk):
    set_pricing(db_session, kiosk, bands=NARROW, bw_single=Decimal("1.50"))
    assert effective_prices(kiosk)["bw_single"] == Decimal("1.50")


def test_a_price_at_the_ceiling_is_allowed(db_session, kiosk):
    set_pricing(
        db_session,
        kiosk,
        bands=NARROW,
        bw_single=Decimal("5.00"),
        bw_double=Decimal("5.00"),
    )
    assert effective_prices(kiosk)["bw_single"] == Decimal("5.00")


def test_a_negative_price_is_refused_even_unbounded(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_pricing(db_session, kiosk, bands=OPEN, bw_single=Decimal("-1"))


def test_nothing_is_written_when_one_price_is_invalid(db_session, kiosk):
    """A rejected request must leave the kiosk exactly as it was, not
    half-updated."""
    set_pricing(db_session, kiosk, bands=OPEN, bw_single=Decimal("2"))

    with pytest.raises(BadRequest):
        set_pricing(
            db_session,
            kiosk,
            bands=NARROW,
            bw_single=Decimal("4"),
            color_single=Decimal("999"),
        )

    assert effective_prices(kiosk)["bw_single"] == Decimal("2.00")


def test_double_sided_cannot_cost_less_than_single_sided(db_session, kiosk):
    """A student would pay less for more paper, on the slower option."""
    with pytest.raises(BadRequest):
        set_pricing(
            db_session,
            kiosk,
            bands=OPEN,
            bw_single=Decimal("4"),
            bw_double=Decimal("3"),
        )


def test_the_double_sided_check_uses_existing_prices_too(db_session, kiosk):
    """Setting only the single-sided price can still break the relationship."""
    set_pricing(db_session, kiosk, bands=OPEN, bw_double=Decimal("3"))
    with pytest.raises(BadRequest):
        set_pricing(db_session, kiosk, bands=OPEN, bw_single=Decimal("5"))


def test_double_sided_cannot_cost_as_much_as_two_single_sided(db_session, kiosk):
    """At 2x, printing two single-sided sheets is cheaper, so duplex becomes a
    trap rather than a saving."""
    with pytest.raises(BadRequest) as caught:
        set_pricing(
            db_session,
            kiosk,
            bands=OPEN,
            bw_single=Decimal("3"),
            bw_double=Decimal("6.01"),
        )
    assert "two" in str(caught.value)


def test_double_sided_at_exactly_twice_single_is_allowed(db_session, kiosk):
    """Same money, half the paper -- not a trap, and some students prefer it."""
    set_pricing(
        db_session, kiosk, bands=OPEN, bw_single=Decimal("3"), bw_double=Decimal("6")
    )
    assert effective_prices(kiosk)["bw_double"] == Decimal("6.00")


def test_double_sided_just_under_twice_single_is_allowed(db_session, kiosk):
    set_pricing(
        db_session,
        kiosk,
        bands=OPEN,
        bw_single=Decimal("3"),
        bw_double=Decimal("5.99"),
    )
    assert effective_prices(kiosk)["bw_double"] == Decimal("5.99")


def test_equal_single_and_double_prices_are_allowed(db_session, kiosk):
    set_pricing(
        db_session, kiosk, bands=OPEN, bw_single=Decimal("3"), bw_double=Decimal("3")
    )
    assert effective_prices(kiosk)["bw_double"] == Decimal("3.00")


def test_setting_no_prices_is_refused(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_pricing(db_session, kiosk, bands=OPEN)


def test_an_unknown_price_field_is_refused(db_session, kiosk):
    with pytest.raises(BadRequest):
        set_pricing(db_session, kiosk, bands=OPEN, a3_single=Decimal("5"))


def test_prices_are_quantised_to_paise(db_session, kiosk):
    set_pricing(db_session, kiosk, bands=OPEN, bw_single=Decimal("2.005"))
    assert effective_prices(kiosk)["bw_single"] == Decimal("2.01")


def test_the_placeholder_band_is_unbounded(db_session, kiosk):
    """Fails open on purpose: a silly price is visible and reversible, unlike a
    misrouted payment."""
    set_pricing(
        db_session,
        kiosk,
        bands=PlatformBand(),
        bw_single=Decimal("999"),
        bw_double=Decimal("1998"),
    )
    assert effective_prices(kiosk)["bw_single"] == Decimal("999.00")
