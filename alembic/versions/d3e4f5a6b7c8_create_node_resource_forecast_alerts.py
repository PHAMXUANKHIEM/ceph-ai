"""create durable CPU/RAM forecast Telegram alert state

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a678
"""

from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a678"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_resource_forecast_alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("first_detected_at", sa.DateTime(), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(), nullable=False),
        sa.Column("last_notified_at", sa.DateTime()),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("current_percent", sa.Float(), nullable=False),
        sa.Column("predicted_percent", sa.Float(), nullable=False),
        sa.Column("hours_to_90", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "cluster_name", "host", "metric",
            name="uq_node_resource_forecast_alert_identity",
        ),
    )
    op.create_index(
        "ix_node_resource_forecast_alerts_cluster_name",
        "node_resource_forecast_alerts",
        ["cluster_name"],
    )
    op.create_index(
        "ix_node_resource_forecast_alerts_host",
        "node_resource_forecast_alerts",
        ["host"],
    )
    op.create_index(
        "ix_node_resource_forecast_alert_status",
        "node_resource_forecast_alerts",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("node_resource_forecast_alerts")
