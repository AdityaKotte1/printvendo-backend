"""What was done, and what needs attention.

Two tables that the backend being replaced also had, and got wrong in the same
way: both existed, and neither was in the path.

`admin_audit_log` was real and was written from 15 of its 94 mutating routes.
Sixteen percent is what "remember to call the audit helper" produces. The rule
here is identical; what is different is that a mutating route with no entry in
`tests/ops/audit_matrix.py` fails the build, so the decision cannot be skipped
by not thinking about it.

`notifications` was admin-scoped by an `is_admin_only` flag that one code path
set and nothing filtered on. Named `AdminAlert` here, because that is what it
is; there is no student-visible notification in this table and no flag that
could make one.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, EnumText
from app.core.ids import IdPrefix, new_id


class AlertSeverity(StrEnum):
    """How much attention this needs, not how bad the underlying thing is.

    Three levels rather than five: an operator triaging a list makes one of
    three decisions -- look now, look today, read later -- and a scale with more
    steps than decisions is a scale nobody applies consistently.
    """

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class AuditEntry(Base):
    """One thing somebody did, with what it looked like before and after.

    `before` and `after` are JSONB rather than text so an admin console can
    query inside them -- "every price change on this kiosk" is a question about
    the contents, not about the row.

    The entity is named by its **public** id. Numeric primary keys never leave
    the database, and an audit trail is read by people; `ksk_7f2...` is a thing
    an admin can paste into a URL, `41` is not.

    Deliberately no `updated_at` and nothing that writes to an existing row. An
    audit entry that can be edited is not an audit entry.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        # "What happened to this kiosk, newest first" is the question an admin
        # console asks constantly, and it is always all three columns. The
        # single-column indexes below serve the other filters.
        Index("ix_audit_log_entity", "entity_type", "entity_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Null when the system did it rather than a person: a scheduled expiry, a
    # webhook settling a payment. That is a real distinction -- "nobody did
    # this" is different from "we did not record who".
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Dotted and stable, e.g. "kiosk.pricing.update", "payment.refund".
    # The prefix is the subject, never the audience -- the same action performed
    # by an owner and by an admin is one action, and recording it as two is how
    # the old backend's reports disagreed with each other.
    action: Mapped[str] = mapped_column(String(80), index=True)

    entity_type: Mapped[str] = mapped_column(String(40), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class AdminAlert(Base):
    """Something an operator needs to look at.

    `dedupe_key` is what stops a kiosk that has been offline for a week
    producing a thousand identical rows. It is unique among *unresolved* alerts
    only -- a partial index -- so the same problem recurring after somebody
    fixed it raises a fresh alert rather than being silently swallowed as a
    duplicate of a closed one.
    """

    __tablename__ = "admin_alerts"
    __table_args__ = (
        # The deduplication rule lives here, not in Python. Partial on
        # `resolved = false`: a condition that recurs after somebody fixed it
        # raises a fresh alert rather than silently bumping a closed one, which
        # a plain unique index would do forever.
        Index(
            "uq_admin_alerts_open_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("resolved = false"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=lambda: new_id(IdPrefix.ALERT)
    )

    severity: Mapped[AlertSeverity] = mapped_column(
        EnumText(AlertSeverity, 12), index=True
    )
    # Dotted, like an action: "kiosk.offline", "payment.unsettled".
    kind: Mapped[str] = mapped_column(String(60), index=True)

    # One sentence, written for the person reading the list.
    summary: Mapped[str] = mapped_column(String(300))
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # What makes a recurring condition one alert rather than a thousand.
    dedupe_key: Mapped[str] = mapped_column(String(120), index=True)

    resolved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Bumped each time the same unresolved condition is reported again, so an
    # operator can tell a blip from something happening every minute.
    occurrences: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
