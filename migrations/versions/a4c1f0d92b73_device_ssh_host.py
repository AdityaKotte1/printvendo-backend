"""where a kiosk's machine can be reached

A kiosk sits behind a shop's NAT, so there is no address the server could work
out on its own. The machine reports one on its heartbeat -- its tailnet name --
and the console shows it so somebody can open a shell without hunting for it.

An identifier, never a credential. It gets an operator as far as their own ssh
client, which then authenticates exactly as it did before.

Revision ID: a4c1f0d92b73
Revises: efb644c8b335
Create Date: 2026-09-02 11:40:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a4c1f0d92b73'
down_revision: str | Sequence[str] | None = 'efb644c8b335'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'kiosk_devices',
        sa.Column('ssh_host', sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('kiosk_devices', 'ssh_host')
