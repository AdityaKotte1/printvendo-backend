"""order refunded_at

Revision ID: 5642769cf7ce
Revises: 5bc3d4fb6418
Create Date: 2026-08-21 01:08:48.397250

"""
# Modernised from Alembic's stock template so generated revisions are already
# ruff-clean: PEP 604 unions and collections.abc.Sequence instead of
# typing.Union / typing.Sequence. Without this, every new revision would
# reintroduce UP006/UP007/UP035 violations and lint would drift into being
# ignored.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5642769cf7ce'
down_revision: str | Sequence[str] | None = '5bc3d4fb6418'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """When every rupee of an order went back.

    Nullable with no backfill: existing rows predate refunds entirely, and a
    default would claim a refund date for orders that were never refunded.
    """
    op.add_column(
        "orders",
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "refunded_at")
