"""add slack_user_id to users

Revision ID: 050b49816c97
Revises: 18fc1fbd1eec
Create Date: 2026-08-08 10:57:43.868693

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "050b49816c97"
down_revision: str | Sequence[str] | None = "18fc1fbd1eec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("slack_user_id", sa.String(), nullable=True))
    op.create_unique_constraint("uq_users_slack_user_id", "users", ["slack_user_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_users_slack_user_id", "users", type_="unique")
    op.drop_column("users", "slack_user_id")
