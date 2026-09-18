"""add blocks_others to work_items

Revision ID: fd3be213bd54
Revises: 050b49816c97
Create Date: 2026-08-11 15:56:25.082887

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fd3be213bd54"
down_revision: str | Sequence[str] | None = "050b49816c97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "work_items",
        sa.Column(
            "blocks_others", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.alter_column("work_items", "blocks_others", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("work_items", "blocks_others")
