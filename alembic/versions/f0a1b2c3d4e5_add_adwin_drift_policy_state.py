"""add durable ADWIN drift policy state

Revision ID: f0a1b2c3d4e5
Revises: e7f8a9b0c1d2
"""

from alembic import op
import sqlalchemy as sa


revision = "f0a1b2c3d4e5"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "node_resource_forecast_runs",
        sa.Column("drift_status", sa.String(32), nullable=False, server_default="INSUFFICIENT_DATA"),
    )
    op.add_column(
        "node_resource_forecast_runs",
        sa.Column("drift_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("node_resource_forecast_runs", sa.Column("drift_reason", sa.Text()))
    op.add_column(
        "node_resource_forecast_runs",
        sa.Column("confidence_multiplier", sa.Float(), nullable=False, server_default="1"),
    )
    op.add_column(
        "node_resource_forecast_runs",
        sa.Column("promotion_blocked", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "node_resource_drift_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(8), nullable=False),
        sa.Column("detector_version", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("last_evaluated_at", sa.DateTime()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("drift_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confidence_multiplier", sa.Float(), nullable=False, server_default="1"),
        sa.Column("promotion_blocked", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "cluster_name", "host", "metric", "detector_version",
            name="uq_node_resource_drift_identity",
        ),
    )
    op.create_index(
        "ix_node_resource_drift_states_cluster_name",
        "node_resource_drift_states", ["cluster_name"],
    )
    op.create_index(
        "ix_node_resource_drift_states_host",
        "node_resource_drift_states", ["host"],
    )
    op.create_index(
        "ix_node_resource_drift_status",
        "node_resource_drift_states", ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_node_resource_drift_status", table_name="node_resource_drift_states")
    op.drop_index("ix_node_resource_drift_states_host", table_name="node_resource_drift_states")
    op.drop_index("ix_node_resource_drift_states_cluster_name", table_name="node_resource_drift_states")
    op.drop_table("node_resource_drift_states")
    for column in (
        "promotion_blocked", "confidence_multiplier", "drift_reason",
        "drift_score", "drift_status",
    ):
        op.drop_column("node_resource_forecast_runs", column)

