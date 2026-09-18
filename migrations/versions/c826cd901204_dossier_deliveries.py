"""dossier deliveries

Revision ID: c826cd901204
Revises: e2a7c5f9d3b1
Create Date: 2026-08-19 15:40:09.153338

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c826cd901204'
down_revision: Union[str, Sequence[str], None] = 'e2a7c5f9d3b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('dossier_deliveries',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner_user_id', sa.String(), nullable=False),
    sa.Column('event_external_id', sa.String(), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('prompt_version', sa.String(), nullable=False),
    sa.Column('talking_points_source', sa.String(), nullable=False),
    sa.Column('card_ref', sa.String(), nullable=True),
    sa.Column('feedback', sa.String(), nullable=False),
    sa.Column('feedback_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_user_id', 'event_external_id', name='uq_dossier_delivery_owner_event')
    )
    op.create_index(op.f('ix_dossier_deliveries_owner_user_id'), 'dossier_deliveries', ['owner_user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_dossier_deliveries_owner_user_id'), table_name='dossier_deliveries')
    op.drop_table('dossier_deliveries')
