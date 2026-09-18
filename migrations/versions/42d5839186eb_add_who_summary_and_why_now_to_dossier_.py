"""add who_summary and why_now to dossier deliveries

Revision ID: 42d5839186eb
Revises: c826cd901204
Create Date: 2026-08-19 17:02:46.749986

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '42d5839186eb'
down_revision: Union[str, Sequence[str], None] = 'c826cd901204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "dossier_deliveries",
        sa.Column("who_summary", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "dossier_deliveries",
        sa.Column("why_now", sa.String(), nullable=False, server_default=""),
    )
    op.alter_column("dossier_deliveries", "who_summary", server_default=None)
    op.alter_column("dossier_deliveries", "why_now", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("dossier_deliveries", "why_now")
    op.drop_column("dossier_deliveries", "who_summary")
