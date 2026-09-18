"""retain normalized event attendees for graph relationships

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    attendees = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.add_column("events", sa.Column("attendees", attendees, nullable=True))
    op.execute("UPDATE events SET attendees = '[]' WHERE attendees IS NULL")
    op.alter_column("events", "attendees", nullable=False, server_default="[]")


def downgrade() -> None:
    op.drop_column("events", "attendees")