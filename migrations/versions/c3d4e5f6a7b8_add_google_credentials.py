"""add per-user google_credentials table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-26 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    scopes = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "google_credentials",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.String(), nullable=False),
        sa.Column("scopes", scopes, nullable=False, server_default="[]"),
        sa.Column("google_email", sa.String(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("google_credentials")
