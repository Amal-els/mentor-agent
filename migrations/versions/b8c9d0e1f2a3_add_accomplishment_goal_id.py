"""add accomplishments.goal_id

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accomplishments",
        sa.Column("goal_id", sa.String(), sa.ForeignKey("goals.id"), nullable=True),
    )
    op.create_index(
        "ix_accomplishments_goal_id", "accomplishments", ["goal_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_accomplishments_goal_id", table_name="accomplishments")
    op.drop_column("accomplishments", "goal_id")
