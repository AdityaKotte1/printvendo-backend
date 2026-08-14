"""baseline

Revision ID: 996b0c1a6c59
Revises:
Create Date: 2026-08-14 21:33:51.051658

This revision creates nothing. It exists so every later migration has a root to
hang off, and so `alembic upgrade head` stamps a fresh database rather than
silently doing nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "996b0c1a6c59"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
