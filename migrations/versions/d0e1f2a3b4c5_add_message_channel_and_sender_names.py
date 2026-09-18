"""add messages.channel_name and messages.sender_display_name

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("channel_name", sa.String(), nullable=True))
    op.add_column(
        "messages", sa.Column("sender_display_name", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("messages", "sender_display_name")
    op.drop_column("messages", "channel_name")
