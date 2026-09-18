"""add owner-scoped knowledge graph

Revision ID: a1b2c3d4e5f6
Revises: 9c1d4e7a2f53, 722f61953cb9
Create Date: 2026-08-25 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = (
    "9c1d4e7a2f53",
    "722f61953cb9",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "graph_nodes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("node_type", sa.String(), nullable=False),
        sa.Column("canonical_key", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("properties", json_type, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id", "node_type", "canonical_key",
            name="uq_graph_node_owner_type_key",
        ),
    )
    op.create_index("ix_graph_nodes_owner_user_id", "graph_nodes", ["owner_user_id"])

    op.create_table(
        "graph_edges",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("from_node_id", sa.String(), sa.ForeignKey("graph_nodes.id"), nullable=False),
        sa.Column("to_node_id", sa.String(), sa.ForeignKey("graph_nodes.id"), nullable=False),
        sa.Column("edge_type", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("status", sa.String(), nullable=False, server_default="confirmed"),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("evidence", json_type, nullable=False, server_default="{}"),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id", "from_node_id", "to_node_id", "edge_type",
            name="uq_graph_edge_owner_nodes_type",
        ),
    )
    op.create_index("ix_graph_edges_owner_user_id", "graph_edges", ["owner_user_id"])
    op.create_index("ix_graph_edges_from_node_id", "graph_edges", ["from_node_id"])
    op.create_index("ix_graph_edges_to_node_id", "graph_edges", ["to_node_id"])


def downgrade() -> None:
    op.drop_index("ix_graph_edges_to_node_id", table_name="graph_edges")
    op.drop_index("ix_graph_edges_from_node_id", table_name="graph_edges")
    op.drop_index("ix_graph_edges_owner_user_id", table_name="graph_edges")
    op.drop_table("graph_edges")
    op.drop_index("ix_graph_nodes_owner_user_id", table_name="graph_nodes")
    op.drop_table("graph_nodes")