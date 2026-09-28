"""persist Loki node-resource data-quality decisions

Revision ID: e7f8a9b0c1d2
Revises: d3e4f5a6b7c8
"""

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d2"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_resource_quality_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(8), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("latest_observed_at", sa.DateTime(), nullable=True),
        sa.Column("age_seconds", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("history_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("coverage_ratio", sa.Float(), nullable=False, server_default="0"),
        sa.Column("longest_gap_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "cluster_name", "host", "metric",
            name="uq_node_resource_quality_identity",
        ),
    )
    op.create_index(
        "ix_node_resource_quality_states_cluster_name",
        "node_resource_quality_states",
        ["cluster_name"],
    )
    op.create_index(
        "ix_node_resource_quality_states_host",
        "node_resource_quality_states",
        ["host"],
    )
    op.create_index(
        "ix_node_resource_quality_status",
        "node_resource_quality_states",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("node_resource_quality_states")
