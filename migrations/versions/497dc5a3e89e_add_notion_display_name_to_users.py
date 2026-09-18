"""add notion_display_name to users

Revision ID: 497dc5a3e89e
Revises: b0854c783433
Create Date: 2026-08-18 10:30:00.000000

Cosmetic-only column (login screen display name), resolved once via a
live Notion API-get-users call at `link-notion` time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "497dc5a3e89e"
down_revision: str | Sequence[str] | None = "b0854c783433"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users", sa.Column("notion_display_name", sa.String(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "notion_display_name")
