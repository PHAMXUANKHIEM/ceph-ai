"""Create bounded RGW aggregate metric history."""

from alembic import op
import sqlalchemy as sa


revision = "m20260921rgwmetrics"
down_revision = "m20260921tgoutbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rgw_metric_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("bytes_total", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_rate_percent", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_p95_ms", sa.Float(), nullable=True),
        sa.Column("top_buckets_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("top_requesters_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("evidence_gaps_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("source", sa.String(length=64), nullable=False, server_default=sa.text("'rgw_access_audit_events'")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rgw_metric_snapshot_cluster_time",
        "rgw_metric_snapshots",
        ["cluster_id", "captured_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_rgw_metric_snapshot_cluster_time", table_name="rgw_metric_snapshots")
    op.drop_table("rgw_metric_snapshots")
