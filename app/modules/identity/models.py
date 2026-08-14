"""Identity tables: users, their roles, and their refresh tokens.

Only this module may import these classes. Everything else goes through the
service functions, so the storage shape stays changeable.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.ids import IdPrefix, new_id
from app.modules.identity.roles import Role


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # What the API exposes. The integer primary key never leaves the database.
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.USER)
    )

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # An anonymous account created by /auth/guest. Not a role: guests hold
    # STUDENT like anyone else. Guests have no wallet.
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Whether the address has been proved to belong to this person.
    #
    # False does NOT block signing in: a bounced or slow verification email
    # would otherwise lock someone out of an account they just created. It is
    # surfaced on /me so the modules that own risky actions can gate on it.
    #
    # Google sign-in sets this True -- Google already proved the address.
    # Users migrated from the old backend are grandfathered True; they have
    # been using the system for months and flipping them to unverified would
    # gate the entire existing user base on cutover night.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    # Row id in the backend being replaced. Nullable, indexed, kept permanently
    # so a number that looks wrong later can be traced to its origin.
    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    roles: Mapped[list["UserRole"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[Role] = mapped_column(String(20))
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="roles")


class EmailVerification(Base):
    """A pending "prove you own this address" token.

    Stored as a hash for the same reason refresh tokens are: a database dump
    must not hand anyone a working link. `used_at` makes a token single-use, so
    a link forwarded or left in a mailbox cannot be replayed.
    """

    __tablename__ = "email_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RefreshToken(Base):
    """A refresh token, stored only as a hash.

    `family_id` ties every token descended from one login together, so a replay
    can revoke the whole chain rather than just the token presented.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    family_id: Mapped[str] = mapped_column(String(32), index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
