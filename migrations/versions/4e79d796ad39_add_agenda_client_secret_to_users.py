"""add agenda_client_secret to users

Revision ID: 4e79d796ad39
Revises: 789abc1406de
Create Date: 2026-08-15 17:44:48.441954

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e79d796ad39"
down_revision: str | Sequence[str] | None = "789abc1406de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users", sa.Column("agenda_client_secret", sa.String(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_users_agenda_client_secret", "users", ["agenda_client_secret"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_users_agenda_client_secret", "users", type_="unique")
    op.drop_column("users", "agenda_client_secret")
