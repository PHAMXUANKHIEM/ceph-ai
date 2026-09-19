"""separate predictive alert lifecycle and notification state

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
"""

from alembic import op
import sqlalchemy as sa


revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        batch_op.add_column(sa.Column("lifecycle_state", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("notification_state", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("state_changed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("state_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("evidence_version", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("consecutive_breach_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("consecutive_healthy_count", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE node_resource_forecast_alerts "
        "SET lifecycle_state = CASE WHEN status = 'DATA_QUALITY' THEN 'DATA_QUALITY' "
        "WHEN status = 'OPEN' THEN 'WARNING' ELSE 'NORMAL' END, "
        "notification_state = CASE WHEN last_notified_at IS NULL THEN 'IDLE' ELSE 'SENT' END, "
        "consecutive_breach_count = 0, consecutive_healthy_count = 0 "
        "WHERE lifecycle_state IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        batch_op.drop_column("consecutive_healthy_count")
        batch_op.drop_column("consecutive_breach_count")
        batch_op.drop_column("evidence_version")
        batch_op.drop_column("state_reason")
        batch_op.drop_column("state_changed_at")
        batch_op.drop_column("notification_state")
        batch_op.drop_column("lifecycle_state")
