"""What an order costs, decided once.

The backend being replaced priced a job in `jobs.py` and added the platform fee
in `payments.py`. Two places, so the number a student was shown and the number
they were charged were assembled by different code, and only one of them was
ever adjusted when something changed.

Here a quote is produced in one function and then **stored on the order**. It is
not recomputed at payment time: a price band edited between "here is your total"
and "pay now" must not quietly change what somebody is charged after they have
seen a number and agreed to it.

**Impressions, not sheets.** A ten-page duplex job prints ten sides on five
sheets. Duplex is cheaper *per side* and that is the whole discount -- charging
by sheets as well would halve the revenue on every duplex job. Paper accounting
uses sheets; pricing uses impressions; `workload()` gives both and they cannot
disagree.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.core.errors import BadRequest
from app.core.money import as_money, sum_money
from app.modules.printing import PrintOptions, workload

# D9, a recorded commercial decision: min(2% of subtotal, ₹2) on every gateway
# payment, including at owner-gateway kiosks, where the owner keeps it. It is
# deliberate and documented in the design spec -- not an oversight to tidy up.
FEE_RATE = Decimal("0.02")
FEE_CAP_INR = Decimal("2.00")

EMPTY_ORDER = "There is nothing in this order."


def gateway_fee(subtotal: Decimal) -> Decimal:
    """The platform fee on a gateway payment."""
    return as_money(min(as_money(subtotal) * FEE_RATE, FEE_CAP_INR))


@dataclass(frozen=True)
class LineQuote:
    """One document's contribution to an order."""

    impressions: int
    sheets: int
    amount_inr: Decimal
    page_count: int


def _rate_key(options: PrintOptions) -> str:
    colour = "color" if options.colour else "bw"
    side = "double" if options.duplex else "single"
    return f"{colour}_{side}"


def quote_line(
    options: PrintOptions, *, total_pages: int, prices: dict[str, Decimal]
) -> LineQuote:
    """Price one document at one kiosk's rates.

    `prices` comes from `kiosks.effective_prices`, so a kiosk that has set no
    price of its own is charged the platform default from one place rather than
    from a default repeated at each call site.
    """
    load = workload(options, total_pages=total_pages)
    rate = as_money(prices[_rate_key(options)])

    return LineQuote(
        impressions=load.impressions,
        sheets=load.sheets,
        amount_inr=as_money(rate * load.impressions),
        page_count=load.pages,
    )


@dataclass(frozen=True)
class OrderQuote:
    """Everything the student is being asked to agree to."""

    lines: tuple[LineQuote, ...]
    subtotal_inr: Decimal
    fee_inr: Decimal
    total_inr: Decimal

    @classmethod
    def build(cls, lines: list[LineQuote], *, pays_by_wallet: bool) -> "OrderQuote":
        """Total the lines and add the fee the payment method attracts.

        Wallet carries no fee (D8): the money is already the platform's, and a
        fee there would be a second cut of it.
        """
        if not lines:
            raise BadRequest(EMPTY_ORDER)

        # Summed once and quantized at the end, so thirty lines of 33 paise come
        # to 9.90 rather than to whatever repeated rounding produces.
        subtotal = sum_money(line.amount_inr for line in lines)
        fee = Decimal("0.00") if pays_by_wallet else gateway_fee(subtotal)

        return cls(
            lines=tuple(lines),
            subtotal_inr=subtotal,
            fee_inr=fee,
            total_inr=as_money(subtotal + fee),
        )
