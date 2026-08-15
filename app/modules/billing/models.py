"""Plans, subscriptions, and the per-owner commercial terms admin can set.

What Printvendo earns from an owned kiosk is the subscription and nothing else.
There is no commission and there are no settlements -- the platform never
handles an owner's print revenue at all. That makes the subscription the whole
commercial relationship, which is why the terms on it are as configurable as
they are.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, EnumText
from app.core.ids import IdPrefix, new_id


class SubscriptionStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Plan(Base):
    """A subscription tier, and the price band it imposes on its kiosks.

    Price floors and ceilings live on the plan because they are a commercial
    term of the plan, not a property of the machine -- the same printer on a
    different plan may charge differently.
    """

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.SUBSCRIPTION)
    )

    name: Mapped[str] = mapped_column(String(100), unique=True)
    monthly_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    max_kiosks: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    # Null on either side means unbounded there.
    price_floor_bw: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_ceiling_bw: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_floor_color: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    price_ceiling_color: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PlanDiscount(Base):
    """The published discount ladder: percent off for paying N months up front.

    This is the list price. An individual owner may be given something different
    -- see OwnerDiscount.
    """

    __tablename__ = "plan_discounts"
    __table_args__ = (
        UniqueConstraint("plan_id", "duration_months", name="uq_plan_duration"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    duration_months: Mapped[int] = mapped_column(Integer)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, server_default="0")

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class OwnerDiscount(Base):
    """A discount negotiated with one owner, overriding the plan's ladder.

    New in this backend (spec D13). The old system could change an owner's
    monthly price, or change a discount for everyone on a plan, but not give one
    owner a different annual rate -- which is the ordinary shape of a real
    commercial negotiation.
    """

    __tablename__ = "owner_discounts"
    __table_args__ = (
        UniqueConstraint("user_id", "duration_months", name="uq_owner_duration"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    duration_months: Mapped[int] = mapped_column(Integer)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    granted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Subscription(Base):
    """One owner's subscription.

    `free_until` is the trial, and it is a money-routing lever rather than a
    billing courtesy: a subscription inside its trial counts as ACTIVE, which is
    half of what kiosk_payment_gate requires to route payments to the owner's
    own Razorpay. Granting a trial therefore lets that owner's kiosk go live and
    collect into their account; the trial expiring suspends it again.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.SUBSCRIPTION)
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"), index=True)

    status: Mapped[SubscriptionStatus] = mapped_column(
        EnumText(SubscriptionStatus, 20),
        default=SubscriptionStatus.PENDING_PAYMENT,
        server_default=SubscriptionStatus.PENDING_PAYMENT.value,
        index=True,
    )

    duration_months: Mapped[int] = mapped_column(Integer)

    # What was actually charged, after every override and discount. Stored
    # rather than recomputed: the plan's price may change later, and an invoice
    # must always say what was really paid.
    monthly_price_charged: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=0, server_default="0"
    )
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))

    # Per-owner commercial terms, both set by an admin.
    negotiated_price: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    free_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # A few days past expiry where the kiosk keeps working, so a late renewal
    # does not take a shop offline over a weekend.
    grace_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    razorpay_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
