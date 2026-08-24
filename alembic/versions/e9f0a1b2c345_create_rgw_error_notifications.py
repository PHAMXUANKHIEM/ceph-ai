"""create immediate RGW error notifications

Revision ID: e9f0a1b2c345
Revises: d8e9f0a1b234
"""
import sqlalchemy as sa
from alembic import op

revision = "e9f0a1b2c345"
down_revision = "d8e9f0a1b234"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("rgw_error_notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("rgw_host", sa.String(255), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
        sa.Column("message", sa.Text(), nullable=False), sa.Column("event_at", sa.DateTime(), nullable=False),
        sa.Column("telegram_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("telegram_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("telegram_error", sa.Text()), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("telegram_sent_at", sa.DateTime()))
    op.create_index("ix_rgw_error_notification_pending", "rgw_error_notifications", ["telegram_sent", "event_at"])

def downgrade() -> None:
    op.drop_index("ix_rgw_error_notification_pending", table_name="rgw_error_notifications")
    op.drop_table("rgw_error_notifications")
