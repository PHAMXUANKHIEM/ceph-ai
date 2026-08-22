"""add node resource forecast learning tables

Revision ID: 5d6e7f8091a2
Revises: 4c5d6e7f8091
"""

from alembic import op
import sqlalchemy as sa

revision = "5d6e7f8091a2"
down_revision = "4c5d6e7f8091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_resource_forecast_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(8), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=False),
        sa.Column("predicted_at", sa.DateTime(), nullable=False),
        sa.Column("target_at", sa.DateTime(), nullable=False),
        sa.Column("current_percent", sa.Float(), nullable=False),
        sa.Column("predicted_percent", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("actual_percent", sa.Float()),
        sa.Column("absolute_error", sa.Float()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime()),
        sa.UniqueConstraint("idempotency_key", name="uq_node_resource_forecast_idempotency"),
    )
    op.create_index("ix_node_resource_forecast_runs_cluster_name", "node_resource_forecast_runs", ["cluster_name"])
    op.create_index("ix_node_resource_forecast_runs_host", "node_resource_forecast_runs", ["host"])
    op.create_index("ix_node_resource_forecast_runs_predicted_at", "node_resource_forecast_runs", ["predicted_at"])
    op.create_index("ix_node_resource_forecast_runs_target_at", "node_resource_forecast_runs", ["target_at"])
    op.create_index("ix_node_resource_forecast_runs_status", "node_resource_forecast_runs", ["status"])
    op.create_index("ix_node_resource_forecast_due", "node_resource_forecast_runs", ["status", "target_at"])

    op.create_table(
        "node_resource_model_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(8), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=False),
        sa.Column("evaluated_count", sa.Integer(), nullable=False),
        sa.Column("mean_absolute_error", sa.Float()),
        sa.Column("last_absolute_error", sa.Float()),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("cluster_name", "host", "metric", "algorithm", "window_hours", name="uq_node_resource_model_identity"),
    )
    op.create_index("ix_node_resource_model_states_cluster_name", "node_resource_model_states", ["cluster_name"])
    op.create_index("ix_node_resource_model_states_host", "node_resource_model_states", ["host"])


def downgrade() -> None:
    op.drop_table("node_resource_model_states")
    op.drop_table("node_resource_forecast_runs")
