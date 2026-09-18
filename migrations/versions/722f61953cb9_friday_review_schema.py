"""friday review schema

Revision ID: 722f61953cb9
Revises: 7f3a9c1e6b02
Create Date: 2026-08-24 18:45:44.285994

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '722f61953cb9'
down_revision: Union[str, Sequence[str], None] = '7f3a9c1e6b02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('accomplishments', sa.Column('skill_category', sa.String(), nullable=True))
    op.add_column(
        'users',
        sa.Column(
            'friday_review_fire_time_local',
            sa.Time(),
            nullable=False,
            server_default='16:00:00',
        ),
    )
    op.create_table(
        'friday_review_deliveries',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('owner_user_id', sa.String(), nullable=False),
        sa.Column('week_start_date', sa.Date(), nullable=False),
        sa.Column('trigger', sa.String(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('skipped_reason', sa.String(), nullable=True),
        sa.Column('prompt_version', sa.String(), nullable=False),
        sa.Column('card_ref', sa.String(), nullable=True),
        sa.Column(
            'proposed_ledger_items',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column('ledger_confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'owner_user_id', 'week_start_date', 'trigger',
            name='uq_friday_review_delivery_owner_week_trigger',
        ),
    )
    op.create_index(
        op.f('ix_friday_review_deliveries_owner_user_id'),
        'friday_review_deliveries',
        ['owner_user_id'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_friday_review_deliveries_owner_user_id'),
        table_name='friday_review_deliveries',
    )
    op.drop_table('friday_review_deliveries')
    op.drop_column('users', 'friday_review_fire_time_local')
    op.drop_column('accomplishments', 'skill_category')
