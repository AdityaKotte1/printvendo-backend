"""whether a shop prints in colour at all

A shop's machine may be a mono laser, or its colour toner may have run out.
Until now nothing on the server knew, so the app offered colour there, priced it
and took the money -- and the agent, which will not send colour work to a mono
printer, refused the job afterwards.

Defaults true, so every kiosk that already exists keeps colour. Switching it off
by default would take colour off the whole estate at deploy time.

Revision ID: b7e2d4a91c60
Revises: a4c1f0d92b73
Create Date: 2026-09-06 10:15:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7e2d4a91c60'
down_revision: str | Sequence[str] | None = 'a4c1f0d92b73'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'kiosks',
        sa.Column(
            'offers_colour',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('kiosks', 'offers_colour')
