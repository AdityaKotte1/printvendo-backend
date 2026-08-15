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
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, EnumText


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
