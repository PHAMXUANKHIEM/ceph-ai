"""persist forecast quality evidence and alert lifecycle transitions

Revision ID: d4f7a1c9e2b6
Revises: a9b8c7d6e5f4
"""

from alembic import op
import sqlalchemy as sa


revision = "d4f7a1c9e2b6"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_runs") as batch_op:
        batch_op.add_column(sa.Column("coverage_ratio", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("max_gap_hours", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("latest_observed_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        batch_op.add_column(sa.Column("coverage_ratio", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("max_gap_hours", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("latest_observed_at", sa.DateTime(), nullable=True))

    op.create_table(
        "node_resource_forecast_transitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("alert_id", sa.String(length=36), nullable=False),
        sa.Column("previous_state", sa.String(length=16), nullable=True),
        sa.Column("new_state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_version", sa.String(length=64), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["node_resource_forecast_alerts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_node_resource_forecast_transition_alert_time",
        "node_resource_forecast_transitions",
        ["alert_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_node_resource_forecast_transition_alert_time",
        table_name="node_resource_forecast_transitions",
    )
    op.drop_table("node_resource_forecast_transitions")

    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        batch_op.drop_column("latest_observed_at")
        batch_op.drop_column("max_gap_hours")
        batch_op.drop_column("coverage_ratio")

    with op.batch_alter_table("node_resource_forecast_runs") as batch_op:
        batch_op.drop_column("latest_observed_at")
        batch_op.drop_column("max_gap_hours")
        batch_op.drop_column("coverage_ratio")
