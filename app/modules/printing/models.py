"""Documents students upload, and the tasks that print them.

The backend being replaced called an uploaded file a "Job" and a dispatch of it
a "PrinterJob", which read as though one were a kind of the other. They are
different things: a Document is a file that exists, a PrintTask is an intention
to put it on paper at a particular machine with particular settings.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, EnumText
from app.core.ids import IdPrefix, new_id


class DocumentState(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    READY = "ready"
    REJECTED = "rejected"
    # The file has been purged by retention. The row survives so a student's
    # order history does not develop holes.
    EXPIRED = "expired"


class TaskState(StrEnum):
    QUEUED = "queued"
    # Claimed by a device and not yet finished. Holds a lease -- see claims.py.
    SENT_TO_DEVICE = "sent_to_device"
    PRINTING = "printing"
    PRINTED = "printed"
    FAILED = "failed"
    # Refused before printing: no paper, kiosk offline, cancelled.
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


# States a task can no longer move out of. Kept as a set rather than repeated
# comparisons, because "is this finished" is asked in several places and they
# must agree.
TERMINAL_TASK_STATES = frozenset(
    {TaskState.PRINTED, TaskState.FAILED, TaskState.CANCELLED}
)


class Document(Base):
    """A file a student uploaded, and what we know about it."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.DOCUMENT)
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    original_filename: Mapped[str] = mapped_column(String(400))
    original_path: Mapped[str | None] = mapped_column(String(600), nullable=True)

    # Ghostscript-normalised copy. This is what gets printed -- never a
    # page-trimmed version, because the page range is applied at the printer and
    # trimming here too would apply it twice.
    normalised_path: Mapped[str | None] = mapped_column(String(600), nullable=True)

    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    state: Mapped[DocumentState] = mapped_column(
        EnumText(DocumentState, 16),
        default=DocumentState.UPLOADED,
        server_default=DocumentState.UPLOADED.value,
        index=True,
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


class PrintTask(Base):
    """One document, one kiosk, one set of settings, printed once.

    The claim mechanism lives in claims.py and depends on two things here:
    `state` and `lease_expires_at`. A task is only ever handed to a device by an
    atomic UPDATE that moves it out of QUEUED, so two requests can never receive
    the same row.
    """

    __tablename__ = "print_tasks"
    __table_args__ = (
        # The claim query filters on (kiosk, state) and orders by position then
        # created_at. Without this index that becomes a sequential scan holding
        # a row lock, which is exactly where a queue starts serialising badly.
        Index("ix_print_tasks_queue", "kiosk_id", "state", "position", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.PRINT_TASK)
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    kiosk_id: Mapped[int] = mapped_column(
        ForeignKey("kiosks.id", ondelete="CASCADE"), index=True
    )

    state: Mapped[TaskState] = mapped_column(
        EnumText(TaskState, 20),
        default=TaskState.QUEUED,
        server_default=TaskState.QUEUED.value,
        index=True,
    )

    # Where this sits among the documents of one order, so a student's files
    # print in the order they chose rather than in insertion order.
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # ── settings, resolved and frozen at order time ──────────────────────────
    # Explicit columns rather than a JSON blob. The old backend stored these as
    # JSON and parsed it independently in four places -- pricing, storage, paper
    # accounting and the agent -- which is how the price charged, the paper
    # deducted and the pages printed became three different opinions.
    colour: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    duplex: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    copies: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    page_range: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # What workload() predicted when the order was priced. Compared against what
    # the device reports so a driver quietly ignoring duplex becomes visible.
    predicted_sheets: Mapped[int] = mapped_column(Integer)
    actual_sheets: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── claim and lease ──────────────────────────────────────────────────────
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A claimed task that goes quiet past this moment is requeued by the sweeper.
    # Deliberately explicit: the alternative is leaving it claimable the whole
    # time, which is what let the old system print a job twice.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    legacy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
