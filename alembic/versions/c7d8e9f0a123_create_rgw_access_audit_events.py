"""create durable RGW access audit events

Revision ID: c7d8e9f0a123
Revises: b1c2d3e4f567
"""
import sqlalchemy as sa
from alembic import op

revision = "c7d8e9f0a123"
down_revision = "b1c2d3e4f567"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rgw_access_audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("rgw_host", sa.String(255), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=True),
        sa.Column("object_key", sa.Text(), nullable=True),
        sa.Column("requester", sa.String(255), nullable=True),
        sa.Column("remote_addr", sa.String(255), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("bytes_sent", sa.BigInteger(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("event_at", sa.DateTime(), nullable=False),
        sa.Column("telegram_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("telegram_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("telegram_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("telegram_sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("fingerprint", name="uq_rgw_access_audit_fingerprint"),
    )
    op.create_index("ix_rgw_access_audit_pending", "rgw_access_audit_events", ["telegram_sent", "event_at"])
    op.create_index("ix_rgw_access_audit_cluster_time", "rgw_access_audit_events", ["cluster_id", "event_at"])


def downgrade() -> None:
    op.drop_index("ix_rgw_access_audit_cluster_time", table_name="rgw_access_audit_events")
    op.drop_index("ix_rgw_access_audit_pending", table_name="rgw_access_audit_events")
    op.drop_table("rgw_access_audit_events")
