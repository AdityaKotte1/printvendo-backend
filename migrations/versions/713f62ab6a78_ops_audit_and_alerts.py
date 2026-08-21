"""ops audit and alerts

Revision ID: 713f62ab6a78
Revises: 5642769cf7ce
Create Date: 2026-08-21 12:01:02.904168

"""
# Modernised from Alembic's stock template so generated revisions are already
# ruff-clean: PEP 604 unions and collections.abc.Sequence instead of
# typing.Union / typing.Sequence. Without this, every new revision would
# reintroduce UP006/UP007/UP035 violations and lint would drift into being
# ignored.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '713f62ab6a78'
down_revision: str | Sequence[str] | None = '5642769cf7ce'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """The audit trail and the alert list."""
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.String(length=40), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"])
    # "What happened to this kiosk" is the question the console asks constantly,
    # and it is always both columns plus a date order.
    op.create_index(
        "ix_audit_log_entity",
        "audit_log",
        ["entity_type", "entity_id", "created_at"],
    )

    op.create_table(
        "admin_alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=24), nullable=False),
        sa.Column("severity", sa.String(length=12), nullable=False),
        sa.Column("kind", sa.String(length=60), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("entity_type", sa.String(length=40), nullable=True),
        sa.Column("entity_id", sa.String(length=40), nullable=True),
        sa.Column("dedupe_key", sa.String(length=120), nullable=False),
        sa.Column("resolved", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("occurrences", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_alerts_public_id", "admin_alerts", ["public_id"], unique=True)
    op.create_index("ix_admin_alerts_severity", "admin_alerts", ["severity"])
    op.create_index("ix_admin_alerts_kind", "admin_alerts", ["kind"])
    op.create_index("ix_admin_alerts_entity_id", "admin_alerts", ["entity_id"])
    op.create_index("ix_admin_alerts_dedupe_key", "admin_alerts", ["dedupe_key"])
    op.create_index("ix_admin_alerts_resolved", "admin_alerts", ["resolved"])
    op.create_index("ix_admin_alerts_created_at", "admin_alerts", ["created_at"])

    # The deduplication rule, in the database rather than in Python. Partial on
    # `resolved = false`, so a kiosk offline for a week is one open alert with a
    # count, while the same condition returning after somebody fixed it raises a
    # fresh one instead of silently bumping a closed row. A plain unique index
    # would make the second outage invisible forever.
    op.create_index(
        "uq_admin_alerts_open_dedupe",
        "admin_alerts",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("resolved = false"),
    )


def downgrade() -> None:
    op.drop_table("admin_alerts")
    op.drop_table("audit_log")
