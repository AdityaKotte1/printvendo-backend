"""What a page costs at a kiosk, and the band the owner may set it within.

An owner sets their own prices, but not freely: their plan carries a floor and a
ceiling, because a race to the bottom devalues every kiosk on the platform and a
kiosk charging ten rupees a page makes the whole product look like a scam.

**The band travels with the prices.** Every read returns both, so no client
keeps its own copy of the plan's limits to drift out of date -- which is what
`printvendo-owner` already relies on.

Where the band comes from is a *billing* question, asked through a protocol
rather than by importing that module. Sub-project 5 supplies the real one.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.core.money import as_money
from app.modules.kiosks.models import Kiosk

# Used when a kiosk has set no price of its own.
# Per **sheet**: `_single` is a sheet printed on one side, `_double` a sheet
# printed on both. So black and white is 2.00 a page single-sided and 1.50 a
# page double-sided -- the discount that makes duplex worth choosing -- while
# colour is 10.00 a page either way, which is the operator's rule: colour costs
# what it costs per side, and the saving there is paper rather than money.
DEFAULT_PRICES: dict[str, Decimal] = {
    "bw_single": Decimal("2.00"),
    "bw_double": Decimal("3.00"),
    "color_single": Decimal("10.00"),
    "color_double": Decimal("20.00"),
}

PRICE_FIELDS = ("bw_single", "bw_double", "color_single", "color_double")

HUMAN_NAMES = {
    "bw_single": "black and white, single-sided",
    "bw_double": "black and white, double-sided",
    "color_single": "colour, single-sided",
    "color_double": "colour, double-sided",
}


@dataclass(frozen=True)
class PriceBand:
    """What an owner is allowed to charge. None means unbounded on that side."""

    floor_bw: Decimal | None
    ceiling_bw: Decimal | None
    floor_color: Decimal | None
    ceiling_color: Decimal | None

    def bounds_for(self, field: str) -> tuple[Decimal | None, Decimal | None]:
        if field.startswith("color"):
            return self.floor_color, self.ceiling_color
        return self.floor_bw, self.ceiling_bw


UNBOUNDED = PriceBand(None, None, None, None)


class BandSource(Protocol):
    """The price band that applies to this kiosk's owner."""

    def band_for(self, db: Session, kiosk: Kiosk) -> PriceBand: ...


class PlatformBand:
    """Stand-in until the billing module lands.

    Returns an unbounded band, which fails *open* -- deliberately, and unlike
    the billing gate in onboarding.py. The difference: an unchecked price band
    lets an owner set a silly price, which is visible, reversible and costs
    nobody their money. An unchecked billing gate silently routes real payments
    to the wrong account. Failing open is only acceptable where the failure is
    loud and recoverable.
    """

    def band_for(self, db: Session, kiosk: Kiosk) -> PriceBand:
        return UNBOUNDED


def effective_prices(kiosk: Kiosk) -> dict[str, Decimal]:
    """What a student is actually charged here, with defaults filled in."""
    return {
        field: (
            as_money(getattr(kiosk, f"price_{field}"))
            if getattr(kiosk, f"price_{field}") is not None
            else DEFAULT_PRICES[field]
        )
        for field in PRICE_FIELDS
    }


def read_pricing(db: Session, kiosk: Kiosk, *, bands: BandSource) -> dict:
    """Prices *and* the band they must sit within, together.

    Returned as one payload on purpose: a client that fetches prices from here
    and limits from somewhere else will eventually show an owner a limit that no
    longer applies.
    """
    band = bands.band_for(db, kiosk)
    return {
        "prices": effective_prices(kiosk),
        "band": {
            "floor_bw": band.floor_bw,
            "ceiling_bw": band.ceiling_bw,
            "floor_color": band.floor_color,
            "ceiling_color": band.ceiling_color,
        },
    }


def _check_within_band(field: str, value: Decimal, band: PriceBand) -> None:
    floor, ceiling = band.bounds_for(field)
    name = HUMAN_NAMES[field]

    if value < 0:
        raise BadRequest(f"The {name} price cannot be negative.")
    if floor is not None and value < floor:
        raise BadRequest(
            f"Your plan does not allow charging less than ₹{floor} for {name}."
        )
    if ceiling is not None and value > ceiling:
        raise BadRequest(
            f"Your plan does not allow charging more than ₹{ceiling} for {name}."
        )


def set_pricing(
    db: Session,
    kiosk: Kiosk,
    *,
    bands: BandSource,
    **prices: Decimal | None,
) -> dict:
    """Set one or more page prices, refusing anything outside the plan's band.

    Every supplied price is validated before *any* is written, so a rejected
    request leaves the kiosk exactly as it was rather than half-updated.
    """
    unknown = set(prices) - set(PRICE_FIELDS)
    if unknown:
        raise BadRequest(f"Unknown price: {', '.join(sorted(unknown))}.")

    supplied = {k: v for k, v in prices.items() if v is not None}
    if not supplied:
        raise BadRequest("Give at least one price to set.")

    band = bands.band_for(db, kiosk)

    normalised: dict[str, Decimal] = {}
    for field, value in supplied.items():
        money = as_money(value)
        _check_within_band(field, money, band)
        normalised[field] = money

    # The duplex price is per *sheet*, not per side: one sheet printed on both
    # sides. That fixes it between two bounds.
    #
    #   below single  -> a student pays less for more paper
    #   above 2x single -> printing two single-sided sheets is cheaper, so
    #                      duplex is a trap rather than a saving. Exactly 2x is
    #                      allowed: same money, half the paper.
    #
    # Checked against the combined picture, not just the supplied values, so
    # raising one price cannot quietly break the relationship with the other.
    combined = {**effective_prices(kiosk), **normalised}
    for single, double, label in (
        ("bw_single", "bw_double", "black and white"),
        ("color_single", "color_double", "colour"),
    ):
        one_side, both_sides = combined[single], combined[double]

        if both_sides < one_side:
            raise BadRequest(
                f"The {label} double-sided price (₹{both_sides}) cannot be lower "
                f"than the single-sided price (₹{one_side}) — a double-sided "
                "sheet uses the same paper and more toner."
            )
        if both_sides > one_side * 2:
            raise BadRequest(
                f"At ₹{both_sides}, {label} double-sided costs more than two "
                f"single-sided sheets (₹{one_side} each), so nobody would "
                f"choose it. Price it at ₹{one_side * 2} or below."
            )

    for field, money in normalised.items():
        setattr(kiosk, f"price_{field}", money)
    db.add(kiosk)

    return read_pricing(db, kiosk, bands=bands)
