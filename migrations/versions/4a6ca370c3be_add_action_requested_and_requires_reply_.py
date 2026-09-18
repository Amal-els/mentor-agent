"""add action_requested and requires_reply to messages

Revision ID: 4a6ca370c3be
Revises: be6d91f0a8c6
Create Date: 2026-08-12 19:15:52.233559

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a6ca370c3be"
down_revision: str | Sequence[str] | None = "be6d91f0a8c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "messages",
        sa.Column(
            "action_requested", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.alter_column("messages", "action_requested", server_default=None)
    op.add_column(
        "messages", sa.Column("requires_reply", sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("messages", "requires_reply")
    op.drop_column("messages", "action_requested")
