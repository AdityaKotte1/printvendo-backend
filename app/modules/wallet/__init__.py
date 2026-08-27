"""The wallet bounded context.

A student's balance, and the ledger that is its record. Import from here, never
from the submodules' internals.

There is deliberately no `hold`. The old backend's `HOLD`/`CAPTURE` pair existed
to model the gap between debiting and enqueueing a print; the `Order` aggregate
closed that gap, so there is nothing left to hold.
"""

from app.modules.wallet.imports import CarryReport, carry_balance
from app.modules.wallet.ledger import (
    balance_of,
    credit,
    debit,
    statement,
    wallet_for,
)
from app.modules.wallet.models import EntryKind, Wallet, WalletEntry

__all__ = [
    "CarryReport",
    "EntryKind",
    "Wallet",
    "WalletEntry",
    "balance_of",
    "carry_balance",
    "credit",
    "debit",
    "statement",
    "wallet_for",
]
