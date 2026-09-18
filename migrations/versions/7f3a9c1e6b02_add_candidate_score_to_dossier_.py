"""add candidate_score to dossier deliveries

Revision ID: 7f3a9c1e6b02
Revises: 42d5839186eb
Create Date: 2026-08-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7f3a9c1e6b02'
down_revision: Union[str, Sequence[str], None] = '42d5839186eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "dossier_deliveries",
        sa.Column("candidate_score", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("dossier_deliveries", "candidate_score")
