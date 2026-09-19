"""Create the append-only predictive forecast alert event table.

Revision ID: m20260919forecastevents
Revises: f5a6b7c8d9e0
"""

from alembic import op
import sqlalchemy as sa


revision = "m20260919forecastevents"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_resource_forecast_alert_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("alert_id", sa.String(length=36), nullable=False),
        sa.Column("cluster_name", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=8), nullable=False),
        sa.Column("from_state", sa.String(length=16), nullable=True),
        sa.Column("to_state", sa.String(length=16), nullable=False),
        sa.Column("notification_state", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["node_resource_forecast_alerts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_node_resource_forecast_alert_events_cluster_name",
        "node_resource_forecast_alert_events", ["cluster_name"], unique=False,
    )
    op.create_index(
        "ix_node_resource_forecast_alert_events_host",
        "node_resource_forecast_alert_events", ["host"], unique=False,
    )
    op.create_index(
        "ix_node_resource_forecast_alert_event_stream_time",
        "node_resource_forecast_alert_events",
        ["cluster_name", "host", "metric", "occurred_at"], unique=False,
    )
    op.create_index(
        "ix_node_resource_forecast_alert_event_state",
        "node_resource_forecast_alert_events", ["to_state", "occurred_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_node_resource_forecast_alert_event_state",
        table_name="node_resource_forecast_alert_events",
    )
    op.drop_index(
        "ix_node_resource_forecast_alert_event_stream_time",
        table_name="node_resource_forecast_alert_events",
    )
    op.drop_index(
        "ix_node_resource_forecast_alert_events_host",
        table_name="node_resource_forecast_alert_events",
    )
    op.drop_index(
        "ix_node_resource_forecast_alert_events_cluster_name",
        table_name="node_resource_forecast_alert_events",
    )
    op.drop_table("node_resource_forecast_alert_events")
