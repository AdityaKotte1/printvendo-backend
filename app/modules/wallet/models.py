"""A student's balance, and the record of how it got there.

Two departures from the backend being replaced, both deliberate:

* **No `status` on a ledger entry.** The old `WalletLedger.status` was
  `PENDING | SUCCESS | FAILED`, so a row might not have happened. Every consumer
  then needed its own opinion about which statuses count, and the ones that
  disagreed were how the balance and its own ledger drifted apart. Here a row
  exists because money moved, so `sum(entries)` *is* the balance.
* **No `HOLD`.** The old `HOLD`/`CAPTURE` pair existed to model the gap between
  `POST /wallet/hold` and the second request that actually enqueued the print.
  The `Order` aggregate closed that gap -- money and print tasks commit
  together -- so there is no gap left to model.

`balance_inr` is a column rather than a sum because a conditional UPDATE needs
something to be conditional on; that is what makes a double-spend impossible.
It is not a second source of truth: the invariant `sum(entries) == balance` is
asserted by a property test, and a dispute is settled from the ledger.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, EnumText
from app.core.ids import IdPrefix, new_id


class EntryKind(StrEnum):
    TOPUP = "topup"
    SPEND = "spend"
    REFUND = "refund"
    # A person at the platform moving money by hand. Always carries a note, so
    # "why is this student ₹50 up" has an answer.
    ADJUSTMENT = "adjustment"
    PROMO = "promo"


class Wallet(Base):
    """One per student, created the first time it is needed.

    Not created by a registration hook: a student who has never topped up has a
    zero balance whether or not a row exists, and creating them lazily means no
    backfill and no "user exists but wallet does not" state to trip over.
    """

    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )

    balance_inr: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), server_default="0.00"
    )

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    entries: Mapped[list["WalletEntry"]] = relationship(
        back_populates="wallet",
        cascade="all, delete-orphan",
        order_by="WalletEntry.created_at",
    )


class WalletEntry(Base):
    """One movement of money. Signed: credits positive, spends negative."""

    __tablename__ = "wallet_entries"
    __table_args__ = (
        # What makes a webhook delivered three times credit once. The old
        # backend had this on `razorpay_payment_id` for top-ups alone; here
        # every kind of entry carries a reference and every kind is protected.
        UniqueConstraint("wallet_id", "reference", name="uq_wallet_entry_reference"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24),
        unique=True,
        index=True,
        default=lambda: new_id(IdPrefix.WALLET_ENTRY),
    )

    wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), index=True
    )

    kind: Mapped[EntryKind] = mapped_column(EnumText(EntryKind, 16), index=True)

    # Signed rupees. A single signed column rather than debit/credit columns:
    # two columns need a rule about which one is populated, and the sum that
    # proves the balance would have to know it.
    amount_inr: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    # The balance immediately after this entry, as returned by the same atomic
    # UPDATE that moved it. Recorded so support can answer "what did they see at
    # three o'clock" without replaying the whole ledger.
    balance_after_inr: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    # What caused it: a Razorpay payment id, an order's public id, a ticket
    # reference for an adjustment. Never null -- an entry nobody can trace is
    # an entry nobody can dispute.
    reference: Mapped[str] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    wallet: Mapped[Wallet] = relationship(back_populates="entries")
