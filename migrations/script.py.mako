"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
# Modernised from Alembic's stock template so generated revisions are already
# ruff-clean: PEP 604 unions and collections.abc.Sequence instead of
# typing.Union / typing.Sequence. Without this, every new revision would
# reintroduce UP006/UP007/UP035 violations and lint would drift into being
# ignored.
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
