"""add dm_channel to dossier deliveries

Revision ID: 9c1d4e7a2f53
Revises: 7f3a9c1e6b02
Create Date: 2026-08-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c1d4e7a2f53'
down_revision: Union[str, Sequence[str], None] = '7f3a9c1e6b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "dossier_deliveries",
        sa.Column("dm_channel", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("dossier_deliveries", "dm_channel")
