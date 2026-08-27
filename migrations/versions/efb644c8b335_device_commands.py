"""device commands

A row per thing an operator has asked a kiosk's machine to do, and a mark on
the device saying whether a stuck printer is why its shop is shut.

Revision ID: efb644c8b335
Revises: 855a720b7f77
Create Date: 2026-08-24 02:20:59.519007

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
revision: str = 'efb644c8b335'
down_revision: str | Sequence[str] | None = '855a720b7f77'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('kiosk_device_commands',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.String(length=24), nullable=False),
    sa.Column('kiosk_id', sa.Integer(), nullable=False),
    sa.Column('requested_by_user_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('state', sa.String(length=16), server_default='queued', nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['kiosk_id'], ['kiosks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_kiosk_device_commands_kiosk_id'), 'kiosk_device_commands', ['kiosk_id'], unique=False)
    op.create_index(op.f('ix_kiosk_device_commands_public_id'), 'kiosk_device_commands', ['public_id'], unique=True)
    op.create_index(op.f('ix_kiosk_device_commands_state'), 'kiosk_device_commands', ['state'], unique=False)
    op.add_column('kiosk_devices', sa.Column('stuck_since', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('kiosk_devices', 'stuck_since')
    op.drop_index(op.f('ix_kiosk_device_commands_state'), table_name='kiosk_device_commands')
    op.drop_index(op.f('ix_kiosk_device_commands_public_id'), table_name='kiosk_device_commands')
    op.drop_index(op.f('ix_kiosk_device_commands_kiosk_id'), table_name='kiosk_device_commands')
    op.drop_table('kiosk_device_commands')
