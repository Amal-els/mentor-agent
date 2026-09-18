"""drop hris_employee_id from users

Revision ID: e2a7c5f9d3b1
Revises: d1c4f6a8b9e2
Create Date: 2026-08-18 13:00:00.000000

HRIS/BambooHR ingestion (app/ingest/hris_normalize.py, hris_source.py,
LiveBambooHrClient) was removed — Notion's own "Manager" people
property (app/ingest/notion_pair_sync.py) is now this app's only
org-data source for Pair sync, and User rows for it are either linked
via `mentor link-notion` or auto-provisioned (notion_person_id,
migration d1c4f6a8b9e2). hris_employee_id has no remaining reader.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a7c5f9d3b1"
down_revision: str | Sequence[str] | None = "d1c4f6a8b9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("uq_users_hris_employee_id", "users", type_="unique")
    op.drop_column("users", "hris_employee_id")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("users", sa.Column("hris_employee_id", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_users_hris_employee_id", "users", ["hris_employee_id"]
    )
