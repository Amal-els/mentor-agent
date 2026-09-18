"""add source_echo_of to messages

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-28 16:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add source_echo_of JSONB column to messages.

    Stores the structured origin reference for Gmail notification emails
    (e.g. {'source': 'github', 'external_id': 'org/repo#42'}).
    Populated at ingest time by app/ingest/email_echo.py before raw email
    text (subject/body) is stripped per the AGENT.md privacy rule.
    Used by app/salience/assemble.py to suppress redundant echo entries
    from the shortlist when the origin item is already present in DB.
    """
    op.add_column(
        "messages",
        sa.Column("source_echo_of", JSONB, nullable=True),
    )


def downgrade() -> None:
    """Remove source_echo_of column from messages."""
    op.drop_column("messages", "source_echo_of")
