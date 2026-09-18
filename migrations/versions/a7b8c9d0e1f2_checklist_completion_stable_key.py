"""checklist_completions: key on (source, external_id) not item_id

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-26 00:00:00.000000

item_id was the underlying WorkItem/Message row's own DB id — found live:
seed_live's Google-Docs reset-then-refetch cycle runs every cron_scheduler
poll (every 60s, all day) and deletes + re-inserts those rows with a new
id each time, so a completion keyed on item_id went stale within a
minute. Replaced with (source, external_id), the natural key that
actually survives that churn (see app/salience/checklist.py's own
docstring). This table is hours old with no real data worth migrating —
existing rows are dropped rather than backfilled.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No real backfill possible — item_id pointed at rows this table's own
    # bug (see revision docstring) had already made unreliable.
    op.execute("DELETE FROM checklist_completions")
    op.drop_constraint(
        "uq_checklist_completion_owner_item", "checklist_completions", type_="unique"
    )
    op.drop_column("checklist_completions", "item_id")
    op.add_column(
        "checklist_completions", sa.Column("source", sa.String(), nullable=True)
    )
    op.add_column(
        "checklist_completions", sa.Column("external_id", sa.String(), nullable=True)
    )
    op.create_unique_constraint(
        "uq_checklist_completion_owner_item",
        "checklist_completions",
        ["owner_user_id", "item_type", "source", "external_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_checklist_completion_owner_item", "checklist_completions", type_="unique"
    )
    op.drop_column("checklist_completions", "external_id")
    op.drop_column("checklist_completions", "source")
    op.add_column(
        "checklist_completions",
        sa.Column("item_id", sa.String(), nullable=False, server_default=""),
    )
    op.create_unique_constraint(
        "uq_checklist_completion_owner_item",
        "checklist_completions",
        ["owner_user_id", "item_type", "item_id"],
    )
