"""The order aggregate.

One student, one kiosk, N documents, one payment, N print tasks. The backend
being replaced had no such row: `Job`, `PrinterJob` and `Payment` each pointed
independently at a user and a printer, and **nothing owned the transaction**.
`POST /wallet/hold` marked a payment PAID and created no print job, so the
caller had to remember a second call or the student had paid for nothing.

Two shapes here are deliberate reactions to that:

* An order **stores what it quoted** -- subtotal, fee, total. Recomputing at
  payment time would let a price band edited in between silently change what
  somebody is charged after they have agreed to a number.
* An item's `document_id` is **nullable**, with a `kind` beside it. The old
  `Payment.job_id` was NOT NULL, which is why a payment could not span three
  files and why a whole second endpoint (`/wallet/hold/multi`) existed to work
  around it. Paper-shop purchases become an item with no document rather than a
  fake print job. Only DOCUMENT is implemented today; the column is here so the
  shape does not foreclose the other, not as speculation.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, EnumText
from app.core.ids import IdPrefix, new_id


class OrderState(StrEnum):
    # Being assembled. Nothing is owed and nothing is reserved.
    DRAFT = "draft"
    AWAITING_PAYMENT = "awaiting_payment"
    PAID = "paid"
    # Every task handed to a device.
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    # Never paid for, and too old to be.
    EXPIRED = "expired"
    REFUNDED = "refunded"
    # Some documents printed and some did not. A real outcome, not an error:
    # the student is owed a partial refund and the operator needs to see it.
    PARTIALLY_FAILED = "partially_failed"


# Once here, an order cannot be paid, re-paid, or dispatched. Kept as a set
# because "is this finished" is asked in several places and they must agree.
SETTLED_ORDER_STATES = frozenset(
    {
        OrderState.COMPLETED,
        OrderState.EXPIRED,
        OrderState.REFUNDED,
        OrderState.PARTIALLY_FAILED,
    }
)


class PaymentMethod(StrEnum):
    WALLET = "wallet"
    GATEWAY = "gateway"


class ItemKind(StrEnum):
    DOCUMENT = "document"
    # A physical purchase from the kiosk's paper shop. The old backend modelled
    # these as print jobs with no file, which then appeared in print queues.
    SHOP_ITEM = "shop_item"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        # "my orders, newest first" is the student app's home screen.
        Index("ix_orders_user_recent", "user_id", "created_at"),
        # "what is happening at my kiosk" is the owner dashboard's.
        Index("ix_orders_kiosk_state", "kiosk_id", "state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.ORDER)
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="RESTRICT"), index=True
    )

    state: Mapped[OrderState] = mapped_column(
        EnumText(OrderState, 20),
        default=OrderState.DRAFT,
        server_default=OrderState.DRAFT.value,
        index=True,
    )

    # ── what was quoted, frozen ──────────────────────────────────────────────
    subtotal_inr: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    fee_inr: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    total_inr: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    payment_method: Mapped[PaymentMethod | None] = mapped_column(
        EnumText(PaymentMethod, 12), nullable=True
    )
    # Which Razorpay account collected. Recorded at placement from the one gate,
    # so a refund months later goes back the way the money came rather than
    # being re-derived from a kiosk whose configuration has since changed.
    gateway: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Nullable-unique, exactly as the old `razorpay_payment_id` was: the unique
    # constraint is what makes a replayed webhook a no-op rather than a second
    # credit.
    payment_reference: Mapped[str | None] = mapped_column(
        String(80), unique=True, nullable=True, index=True
    )

    # An unpaid order does not hold its paper forever.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItem.position",
    )


class OrderItem(Base):
    """One line of an order: what was asked for, and what it cost.

    The print settings are copied here rather than referenced, because they are
    part of what was paid for. A student who reprints the same document with
    different settings has bought something else.
    """

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )

    # The student's chosen order, so their files print in the order they picked.
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    kind: Mapped[ItemKind] = mapped_column(
        EnumText(ItemKind, 16),
        default=ItemKind.DOCUMENT,
        server_default=ItemKind.DOCUMENT.value,
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    colour: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    duplex: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    copies: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    page_range: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # What the quote counted. Stored so an invoice can be reproduced exactly,
    # rather than recalculated years later against a document that retention has
    # since purged.
    page_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    impressions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sheets: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    amount_inr: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    order: Mapped[Order] = relationship(back_populates="items")
