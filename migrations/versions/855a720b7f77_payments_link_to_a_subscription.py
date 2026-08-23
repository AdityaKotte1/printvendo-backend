"""payments link to a subscription

Revision ID: 855a720b7f77
Revises: 777766be9bfe
Create Date: 2026-08-23 15:41:42.385589

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
revision: str = '855a720b7f77'
down_revision: str | Sequence[str] | None = '777766be9bfe'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Named, where autogenerate left it anonymous. Postgres would have invented a
# name and the downgrade could not drop what it could not name --
# `test_downgrade_to_base_removes_everything` failed on exactly that.
FK_NAME = "fk_payments_subscription_id_subscriptions"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('payments', sa.Column('subscription_id', sa.Integer(), nullable=True))
    op.create_index(
        op.f('ix_payments_subscription_id'), 'payments', ['subscription_id'], unique=False
    )
    op.create_foreign_key(
        FK_NAME,
        'payments',
        'subscriptions',
        ['subscription_id'],
        ['id'],
        ondelete='RESTRICT',
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(FK_NAME, 'payments', type_='foreignkey')
    op.drop_index(op.f('ix_payments_subscription_id'), table_name='payments')
    op.drop_column('payments', 'subscription_id')
