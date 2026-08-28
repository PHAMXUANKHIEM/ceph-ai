"""create durable node resource forecast alert state

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a789
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a789"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "node_resource_forecast_alerts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("first_detected_at", sa.DateTime(), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("current_percent", sa.Float(), nullable=False),
        sa.Column("predicted_percent", sa.Float(), nullable=False),
        sa.Column("hours_to_90", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cluster_name", "host", "metric", name="uq_node_resource_forecast_alert_identity"),
    )
    op.create_index("ix_node_resource_forecast_alerts_cluster_name", "node_resource_forecast_alerts", ["cluster_name"], unique=False)
    op.create_index("ix_node_resource_forecast_alerts_host", "node_resource_forecast_alerts", ["host"], unique=False)
    op.create_index("ix_node_resource_forecast_alert_status", "node_resource_forecast_alerts", ["status"], unique=False)


def downgrade():
    op.drop_index("ix_node_resource_forecast_alert_status", table_name="node_resource_forecast_alerts")
    op.drop_index("ix_node_resource_forecast_alerts_host", table_name="node_resource_forecast_alerts")
    op.drop_index("ix_node_resource_forecast_alerts_cluster_name", table_name="node_resource_forecast_alerts")
    op.drop_table("node_resource_forecast_alerts")
