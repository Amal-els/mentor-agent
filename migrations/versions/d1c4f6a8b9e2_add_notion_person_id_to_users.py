"""add notion_person_id and notion_access_revoked_at to users

Revision ID: d1c4f6a8b9e2
Revises: ab312fa433cc
Create Date: 2026-08-18 12:00:00.000000

Supports auto-provisioning/auto-revoking User rows from a live Notion
Objectives Owner/Manager directory scan (app/ingest/notion_user_
provision.py) instead of requiring `mentor link-notion` for every new
person.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1c4f6a8b9e2"
down_revision: str | Sequence[str] | None = "ab312fa433cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("notion_person_id", sa.String(), nullable=True))
    op.add_column(
        "users",
        sa.Column("notion_access_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_users_notion_person_id", "users", ["notion_person_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_users_notion_person_id", "users", type_="unique")
    op.drop_column("users", "notion_access_revoked_at")
    op.drop_column("users", "notion_person_id")
