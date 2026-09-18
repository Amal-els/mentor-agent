"""add feedback_events item_type/consolidated_at and weights n_signals

Revision ID: be6d91f0a8c6
Revises: fd3be213bd54
Create Date: 2026-08-12 13:01:06.176205

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "be6d91f0a8c6"
down_revision: str | Sequence[str] | None = "fd3be213bd54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "feedback_events",
        sa.Column(
            "item_type", sa.String(), nullable=False, server_default="work_item"
        ),
    )
    op.alter_column("feedback_events", "item_type", server_default=None)
    op.add_column(
        "feedback_events",
        sa.Column("consolidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "weights",
        sa.Column("n_signals", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("weights", "n_signals", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("weights", "n_signals")
    op.drop_column("feedback_events", "consolidated_at")
    op.drop_column("feedback_events", "item_type")
