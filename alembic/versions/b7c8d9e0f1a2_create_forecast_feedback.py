"""create append-only operator feedback for predictive alerts

Revision ID: b7c8d9e0f1a2
Revises: a2b3c4d5e6f7
"""

from alembic import op
import sqlalchemy as sa


revision = "b7c8d9e0f1a2"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_resource_forecast_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("alert_id", sa.String(length=36), nullable=False),
        sa.Column("verdict", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("impact_percent", sa.Float(), nullable=True),
        sa.Column("incident_id", sa.String(length=36), nullable=True),
        sa.Column("remediation_case_id", sa.String(length=36), nullable=True),
        sa.Column("submitted_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["node_resource_forecast_alerts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_node_resource_forecast_feedback_alert_created", "node_resource_forecast_feedback", ["alert_id", "created_at"])
    op.create_index("ix_node_resource_forecast_feedback_verdict", "node_resource_forecast_feedback", ["verdict"])
    op.create_index("ix_node_resource_forecast_feedback_incident_id", "node_resource_forecast_feedback", ["incident_id"])
    op.create_index("ix_node_resource_forecast_feedback_remediation_case_id", "node_resource_forecast_feedback", ["remediation_case_id"])


def downgrade() -> None:
    op.drop_index("ix_node_resource_forecast_feedback_remediation_case_id", table_name="node_resource_forecast_feedback")
    op.drop_index("ix_node_resource_forecast_feedback_incident_id", table_name="node_resource_forecast_feedback")
    op.drop_index("ix_node_resource_forecast_feedback_verdict", table_name="node_resource_forecast_feedback")
    op.drop_index("ix_node_resource_forecast_feedback_alert_created", table_name="node_resource_forecast_feedback")
    op.drop_table("node_resource_forecast_feedback")
