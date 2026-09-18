"""add setup_token to users

Revision ID: ab312fa433cc
Revises: 497dc5a3e89e
Create Date: 2026-08-18 11:05:00.000000

One-time self-service setup-link fields (POST /webhooks/request-setup-
link + /webhooks/complete-setup), letting a linked Notion identity get
their agenda_client_secret by email instead of an admin running
`mentor link-agenda-client` directly.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ab312fa433cc"
down_revision: str | Sequence[str] | None = "497dc5a3e89e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("setup_token", sa.String(), nullable=True))
    op.add_column(
        "users",
        sa.Column("setup_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_users_setup_token", "users", ["setup_token"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_users_setup_token", "users", type_="unique")
    op.drop_column("users", "setup_token_expires_at")
    op.drop_column("users", "setup_token")
