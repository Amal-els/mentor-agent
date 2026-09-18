"""scope agenda item dedup uniqueness by visibility

Revision ID: c556b944a1bb
Revises: 4e79d796ad39
Create Date: 2026-08-15 17:48:05.031769

Necessary companion to the append_agenda_item dedup-visibility fix
(composition review finding 2): the old uq_agenda_item_report_source_link
index was unique on (report_user_id, source_link) alone, so a non-visible
existing row (e.g. report_only) for a given source_link made it
impossible to INSERT a second, visible row for the SAME source_link even
after the dedup lookup correctly treated the non-visible row as "no
match" — the write would still fail with an IntegrityError, which is
itself an existence-oracle leak (a caller who cannot see the row can
still tell one exists, from the failure). Re-scoping the uniqueness to
(report_user_id, source_link, visibility) keeps the same "one canonical
row per source_link" invariant WITHIN a visibility bucket (still exactly
one open report_only row, one open manager_only row, one open shared row
per report+source_link) while allowing the report_only and the new
visible row to coexist.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c556b944a1bb"
down_revision: str | Sequence[str] | None = "4e79d796ad39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("uq_agenda_item_report_source_link", table_name="agenda_items")
    op.create_index(
        "uq_agenda_item_report_source_link_visibility",
        "agenda_items",
        ["report_user_id", "source_link", "visibility"],
        unique=True,
        postgresql_where=sa.text("source_link IS NOT NULL AND status != 'resolved'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_agenda_item_report_source_link_visibility", table_name="agenda_items"
    )
    op.create_index(
        "uq_agenda_item_report_source_link",
        "agenda_items",
        ["report_user_id", "source_link"],
        unique=True,
        postgresql_where=sa.text("source_link IS NOT NULL AND status != 'resolved'"),
    )
