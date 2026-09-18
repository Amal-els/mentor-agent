"""add checklist_completions

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "checklist_completions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "owner_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("item_type", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ai_response", sa.String(), nullable=False, server_default=""),
        sa.UniqueConstraint(
            "owner_user_id",
            "item_type",
            "item_id",
            name="uq_checklist_completion_owner_item",
        ),
    )
    op.create_index(
        "ix_checklist_completions_owner_user_id",
        "checklist_completions",
        ["owner_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_checklist_completions_owner_user_id", table_name="checklist_completions"
    )
    op.drop_table("checklist_completions")
