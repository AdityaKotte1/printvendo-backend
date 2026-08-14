"""Money is rupees, Decimal, two places, ROUND_HALF_UP.

float is never a valid monetary type here. as_money rejects it outright rather
than silently accepting a value that has already lost precision.
"""

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")


def as_money(value: Decimal | int | str) -> Decimal:
    """Coerce to a two-place Decimal, rounding half up."""
    if isinstance(value, float):
        raise TypeError("float is not a valid money value; use Decimal or str")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_paise(value: Decimal) -> int:
    """Rupees to integer paise, for the Razorpay API."""
    return int(as_money(value) * 100)


def from_paise(paise: int) -> Decimal:
    """Integer paise back to rupees."""
    return as_money(Decimal(paise) / 100)


def sum_money(values: Iterable[Decimal]) -> Decimal:
    """Sum, quantized once at the end."""
    total = Decimal("0")
    for value in values:
        total += value
    return as_money(total)
