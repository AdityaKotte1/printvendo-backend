"""Carrying a wallet balance over from the backend being replaced.

Cutover has to move student money, and student money is the one thing nobody
can be asked to re-enter.

**Only the closing balance comes across.** Not the transaction history: the
legacy `wallet_ledger` disagreed with its own `wallets.balance` -- its `status`
column is how the two came apart -- so replaying it would mean deciding, per
row, which of two numbers a student is owed. The balance is what they can see in
the app today, so it is what we owe them, and it is the only figure this needs.
A student who wants their old statement asks, and somebody answers from the
retained dump. **That means the dump has to be retained**, which is an
operational commitment rather than a detail.

**It arrives as one entry, not as a bare number.** `balance_inr` is a column so
that a conditional UPDATE has something to be conditional on, but the invariant
`sum(entries) == balance` is what makes the ledger the thing a dispute is
settled from. A balance with no entry behind it would be the first row in this
system that money cannot be traced to.

**Not through `credit`.** This is a record of money that already exists rather
than a request to add some, and it goes through the module's own movement path
with the funds check off for the same reason.

**Re-running is safe, and it has to be.** A cutover gets interrupted and
restarted. The reference is derived from the account, so `UNIQUE (wallet_id,
reference)` makes a second pass a no-op -- and the gap is measured against the
balance that is actually there, never accumulated, so a partial import
completes rather than doubling.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import BadRequest
from app.core.money import as_money
from app.modules.wallet.ledger import _move, balance_of, wallet_for
from app.modules.wallet.models import EntryKind, WalletEntry

CANNOT_OWE_US = (
    "That wallet has a negative balance in the legacy data. Nothing in this "
    "system can spend below zero, so a person has to look at it rather than "
    "have it quietly become a debt."
)

CARRIED_NOTE = "Balance carried over from the previous system at cutover."


@dataclass(frozen=True)
class CarryReport:
    """What happened to one wallet, so the cutover can be read afterwards."""

    carried: Decimal
    # False when the balance was already right -- a second pass, or an account
    # that had nothing to carry.
    wrote_entry: bool


def carry_balance(
    db: Session,
    *,
    user_id: int,
    balance: Decimal,
) -> CarryReport:
    """Bring this student's remaining money across, once.

    Returns what was done rather than logging it: the caller is a migration
    that has to produce a report somebody signs off, and a figure it had to
    scrape out of logs would not be one.
    """
    balance = as_money(balance)
    if balance < Decimal("0.00"):
        raise BadRequest(CANNOT_OWE_US)

    wallet = wallet_for(db, user_id=user_id)

    # Measured against the balance that is there, not assumed to be zero. A
    # second pass then finds nothing to do, and a pass that follows a partial
    # one moves only the difference instead of doubling the money.
    gap = as_money(balance - balance_of(db, user_id=user_id))
    if gap == Decimal("0.00"):
        return CarryReport(carried=Decimal("0.00"), wrote_entry=False)

    # Numbered, so a later pass that finds a fresh gap can close it without
    # colliding with the entry this one wrote.
    written = db.execute(
        select(func.count(WalletEntry.id)).where(
            WalletEntry.wallet_id == wallet.id,
            WalletEntry.kind == EntryKind.ADJUSTMENT,
        )
    ).scalar_one()

    _move(
        db,
        wallet=wallet,
        kind=EntryKind.ADJUSTMENT,
        amount=gap,
        reference=f"migration:balance:{user_id}:{written}",
        note=CARRIED_NOTE,
        # A record of money that already exists, not a request to add it.
        require_funds=False,
    )
    db.flush()

    return CarryReport(carried=gap, wrote_entry=True)
