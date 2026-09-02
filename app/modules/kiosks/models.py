"""Kiosk tables.

The backend being replaced had one `printers` table of thirty columns holding
four unrelated things: what the business relationship is, how the Pi
authenticates, what a page costs, and how much paper is left. They are split
here by what changes them and who may change them -- a refiller writes paper and
nothing else, and that is far easier to enforce when paper is its own row.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
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
from app.modules.kiosks.enums import (
    AssignmentRole,
    DeviceCommandKind,
    DeviceCommandState,
    DeviceStatus,
    KioskType,
    OnboardingStage,
)

DEFAULT_PAPER_CAPACITY = 250


class Kiosk(Base):
    __tablename__ = "kiosks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.KIOSK)
    )

    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)

    kiosk_type: Mapped[KioskType] = mapped_column(
        EnumText(KioskType, 20),
        default=KioskType.PLATFORM,
        server_default=KioskType.PLATFORM.value,
    )
    onboarding_stage: Mapped[OnboardingStage] = mapped_column(
        EnumText(OnboardingStage, 24),
        default=OnboardingStage.REGISTERED,
        server_default=OnboardingStage.REGISTERED.value,
        index=True,
    )
    onboarding_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    # Whether student wallet balance may be spent here. Top-ups land in the
    # platform's Razorpay, so spending at an owner-gateway kiosk would mean the
    # platform keeps the cash while the owner prints for free. Defaults False:
    # wrong restrictively costs a student one payment method, wrong permissively
    # loses the owner money.
    accepts_wallet: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

    location_description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Page prices in rupees. Null means "fall back to the platform default".
    # Bounded by the owner's plan -- see pricing.py.
    price_bw_single: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_bw_double: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_color_single: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )
    price_color_double: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2), nullable=True
    )

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    device: Mapped["KioskDevice | None"] = relationship(
        back_populates="kiosk", uselist=False, cascade="all, delete-orphan"
    )
    paper: Mapped["KioskPaper | None"] = relationship(
        back_populates="kiosk", uselist=False, cascade="all, delete-orphan"
    )


class KioskDevice(Base):
    """The Raspberry Pi (or PC) running the agent at a kiosk."""

    __tablename__ = "kiosk_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.DEVICE)
    )

    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), unique=True, index=True
    )

    # The identifier the agent presents. Distinct from public_id: this one is
    # baked into a device's config file and survives re-registration.
    device_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Only ever a hash. A database dump must not yield a token that can pull
    # print jobs.
    token_hash: Mapped[str] = mapped_column(String(64), index=True)

    status: Mapped[DeviceStatus] = mapped_column(
        EnumText(DeviceStatus, 16),
        default=DeviceStatus.OFFLINE,
        server_default=DeviceStatus.OFFLINE.value,
    )
    capabilities: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    agent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The name this machine can be reached by on the tailnet, as the machine
    # itself reports it. Recorded rather than derived: a kiosk sits behind a
    # shop's NAT and there is no address the server could work out on its own.
    #
    # An identifier, never a credential. It gets somebody as far as their own
    # ssh client, which then has to authenticate the way it always did -- this
    # opens no door, it just says where the door is.
    ssh_host: Mapped[str | None] = mapped_column(String(200), nullable=True)
    agent_update_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When this machine last told us it could not get a job out of the printer.
    #
    # It is on the device rather than a flag on the kiosk because it also says
    # *we* are the reason the shop is closed. An owner who puts their own shop
    # into maintenance to change a toner cartridge must not have it reopened by
    # a printer clearing its queue; this column is what tells those two cases
    # apart, and clearing it is what lets the shop reopen by itself.
    stuck_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    kiosk: Mapped[Kiosk] = relationship(back_populates="device")


class DeviceEnrolment(Base):
    """A one-time code that lets an agent claim a kiosk.

    Without this, `POST /v1/device/register` would be an open door: anyone who
    guessed a kiosk id could attach their own machine to a shop and start
    pulling other people's documents.

    The operator generates a code for one kiosk, SSHes into that Pi, and the
    agent spends it once. What comes back is a device token, so the long-lived
    credential is never typed, pasted, or read by a person -- and the code that
    was typed is worthless a moment later.
    """

    __tablename__ = "device_enrolments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KioskPaper(Base):
    """How much paper is in the tray, and the warning throttle state.

    Stored as sheets *used* against a capacity, because that is what the machine
    reports. Everyone who talks about it thinks in sheets *remaining*, so the
    service layer converts -- see paper.py.
    """

    __tablename__ = "kiosk_paper"

    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), primary_key=True
    )

    capacity: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_PAPER_CAPACITY,
        server_default=str(DEFAULT_PAPER_CAPACITY),
    )
    used: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Throttle state for low-paper email. Cleared on refill, or the next low
    # cycle stays silent.
    warning_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_warning_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    kiosk: Mapped[Kiosk] = relationship(back_populates="paper")


class KioskAssignment(Base):
    """Who is attached to a kiosk, and in what capacity.

    Replaces two structurally identical tables (`printer_owners` and
    `printer_refillers`). A person may legitimately hold both roles at one
    kiosk -- a small shop's owner refills their own paper.
    """

    __tablename__ = "kiosk_assignments"
    __table_args__ = (
        UniqueConstraint("kiosk_id", "user_id", "role", name="uq_kiosk_user_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[AssignmentRole] = mapped_column(EnumText(AssignmentRole, 16), index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KioskFavourite(Base):
    """A shop a student saved.

    The one table here that records something about a *student* rather than
    about a shop's operation, so it is deliberately narrow: it answers "which
    shops did this person save" and nothing else. There is no read in the other
    direction -- who saved this shop is a question about students, asked from
    the kiosk's side, and an owner has no business asking it. Reversing that
    would need a new function, which is the point.
    """

    __tablename__ = "kiosk_favourites"
    __table_args__ = (
        UniqueConstraint("user_id", "kiosk_id", name="uq_favourite_user_kiosk"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # CASCADE, unlike an order line: a saved shop is a shortcut, not a record.
    # A retired kiosk should drop out of everybody's list rather than keep a row
    # that resolves to nothing.
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PaperRefillLog(Base):
    """One row per paper change, whoever made it.

    The audit trail has to be complete regardless of whether an owner, a
    refiller or an admin touched the tray -- otherwise "who let this kiosk run
    dry" has no answer.
    """

    __tablename__ = "paper_refill_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    sheets_added: Mapped[int] = mapped_column(Integer)
    capacity_at_change: Mapped[int] = mapped_column(Integer)
    used_before_change: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class StaffInvite(Base):
    """A pending "come and refill my kiosk" invitation.

    An owner names an email address; nothing is bound and no personal detail is
    disclosed until the invitee accepts. That is what closes the cross-tenant
    enumeration hole in the old backend, where an owner could attach any
    refiller in the platform to their kiosk and then read their name and email.
    """

    __tablename__ = "staff_invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )
    invited_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )

    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[AssignmentRole] = mapped_column(EnumText(AssignmentRole, 16))

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KioskDeviceCommand(Base):
    """Something an operator has asked the machine at a kiosk to do.

    A row rather than a message pushed down the socket, for the same reason a
    print task is a row: the socket carries a wake and never work. A command
    sent over a connection that drops mid-flight is a restart nobody can tell
    happened, and a reconnect overlapping a publish would run it twice.

    It is deliberately not a print task. A print task is owed to a student who
    has paid, is claimed under a lease and is retried; a command is an
    operator's request that goes stale, and running a stale one restarts a shop
    that has been working again for an hour.
    """

    __tablename__ = "kiosk_device_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24),
        unique=True,
        index=True,
        default=lambda: new_id(IdPrefix.DEVICE_COMMAND),
    )

    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )
    # Who asked. Nullable because the row outlives the account: an admin who
    # has left should not take the record of what they restarted with them.
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    kind: Mapped[DeviceCommandKind] = mapped_column(EnumText(DeviceCommandKind, 32))
    state: Mapped[DeviceCommandState] = mapped_column(
        EnumText(DeviceCommandState, 16),
        default=DeviceCommandState.QUEUED,
        server_default=DeviceCommandState.QUEUED.value,
        index=True,
    )

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
