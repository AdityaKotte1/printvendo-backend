"""Where an owner's money goes, and who is allowed to change that.

The backend being replaced stored `razorpay_key_secret` as a plain String while
its own comment claimed the value was "stored encrypted/hashed". There was no
encryption anywhere in that codebase. A database dump therefore handed over
every kiosk owner's live payment credentials -- which matters immediately,
because the migration for this rewrite starts with pg_dump.

Here the secret is ciphertext produced by app.core.crypto.SecretBox, and no API
ever returns it.
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
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, EnumText
from app.core.ids import IdPrefix, new_id


class ChangeRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    USED = "used"


class KioskPaymentConfig(Base):
    """One owner's Razorpay credentials.

    Keys can be set once and then only replaced through an approved change
    request. That is an anti-fraud control, not bureaucracy: without it, anyone
    who takes over an owner's account can silently redirect every student
    payment at every one of their kiosks to an account they control, and the
    owner would only notice at settlement time -- of which there is none, since
    owners are paid directly.
    """

    __tablename__ = "kiosk_payment_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )

    # The key id is not secret -- it appears in the checkout the student sees.
    razorpay_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Ciphertext. Never a plaintext secret, and never returned by any endpoint.
    razorpay_key_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The webhook signing secret, which Razorpay treats as a separate value from
    # the API key secret -- it is set per webhook in the dashboard, not per key.
    # Owners register their own webhook against their own account, so this is
    # theirs and it is the only thing that can verify their deliveries.
    razorpay_webhook_secret_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    is_configured: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    configured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class PaymentConfigChangeRequest(Base):
    """An owner asking to replace their payment keys, for an admin to review.

    `proof_path` is a file the owner uploads showing the new account is theirs.
    It is served through an authenticated route, never as a static URL -- the
    old admin dashboard built `API_BASE + '/storage/...'`, which 404s silently
    behind an `onerror` handler, so admins were approving these having never
    seen the proof.
    """

    __tablename__ = "payment_config_change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[ChangeRequestStatus] = mapped_column(
        EnumText(ChangeRequestStatus, 16),
        default=ChangeRequestStatus.PENDING,
        server_default=ChangeRequestStatus.PENDING.value,
        index=True,
    )

    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PaymentKind(StrEnum):
    """What the money was for.

    One table for all three because they are the same object -- a sum collected
    through a gateway -- and because the backend being replaced proved the
    alternative. It had three webhook endpoints, one per purpose, each with its
    own signature check and its own idea of what "already handled" meant.
    """

    PRINT_ORDER = "print_order"
    WALLET_TOPUP = "wallet_topup"
    SUBSCRIPTION = "subscription"


class PaymentStatus(StrEnum):
    # Razorpay order opened; the student has not paid yet, or we have not heard.
    CREATED = "created"
    CAPTURED = "captured"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class Payment(Base):
    """One sum collected through a gateway.

    `razorpay_payment_id` is **nullable-unique**, and that constraint is the
    replay guard: a webhook delivered three times can insert the capture once.
    The old backend relied on the same property for top-ups and on nothing at
    all for the other two purposes.

    `gateway` and `kiosk_id` are recorded at creation rather than derived later.
    A refund six months on has to go back to the account that actually collected
    -- re-deriving it from a kiosk whose keys or subscription have changed since
    is how money gets sent to the wrong place.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.PAYMENT)
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[PaymentKind] = mapped_column(EnumText(PaymentKind, 20), index=True)

    # Set for PRINT_ORDER only. A top-up buys balance, not a print.
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # Which kiosk's arrangement collected this, for reconciliation and refunds.
    kiosk_id: Mapped[int | None] = mapped_column(
        ForeignKey("kiosks.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    gateway: Mapped[str] = mapped_column(String(20))

    # The owner whose Razorpay account collected this, or NULL when it was the
    # platform's. Recorded rather than re-derived: owners register their own
    # webhooks, so a delivery arriving at one owner's URL must be checked
    # against the payment it claims to be about, and a kiosk's owner or keys may
    # have changed by the time a webhook or a refund arrives.
    collecting_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    razorpay_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )

    amount_inr: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    refunded_inr: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0.00"), server_default="0.00"
    )

    status: Mapped[PaymentStatus] = mapped_column(
        EnumText(PaymentStatus, 20),
        default=PaymentStatus.CREATED,
        server_default=PaymentStatus.CREATED.value,
        index=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
