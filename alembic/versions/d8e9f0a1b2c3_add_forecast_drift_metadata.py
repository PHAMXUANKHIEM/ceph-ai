"""Persist forecast drift metadata and quality features.

Revision ID: d8e9f0a1b2c3
Revises: c8d9e0f1a2b4
"""

from alembic import op
import sqlalchemy as sa


revision = "d8e9f0a1b2c3"
down_revision = "c8d9e0f1a2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("node_resource_forecast_runs")
    }
    with op.batch_alter_table("node_resource_forecast_runs") as batch_op:
        if "coverage_ratio" not in existing:
            batch_op.add_column(sa.Column("coverage_ratio", sa.Float(), nullable=False, server_default="1"))
        if "max_gap_hours" not in existing:
            batch_op.add_column(sa.Column("max_gap_hours", sa.Float(), nullable=False, server_default="0"))
        if "drift_status" not in existing:
            batch_op.add_column(
                sa.Column("drift_status", sa.String(length=24), nullable=False, server_default="INSUFFICIENT_DATA")
            )
        if "drift_score" not in existing:
            batch_op.add_column(sa.Column("drift_score", sa.Float(), nullable=False, server_default="0"))
        if "drift_reason" not in existing:
            batch_op.add_column(sa.Column("drift_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_runs") as batch_op:
        batch_op.drop_column("drift_reason")
        batch_op.drop_column("drift_score")
        batch_op.drop_column("drift_status")
