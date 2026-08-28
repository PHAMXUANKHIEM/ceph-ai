"""create persisted host metric samples

Revision ID: c4d5e6f7a8b9
Revises: c3d4e5f6a7b8
"""

from alembic import op
import sqlalchemy as sa


revision = "c4d5e6f7a8b9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "host_metric_samples",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("node_name", sa.String(length=255), nullable=True),
        sa.Column("cpu_percent", sa.Float(), nullable=False),
        sa.Column("mem_percent", sa.Float(), nullable=False),
        sa.Column("disk_read_iops", sa.Float(), nullable=False),
        sa.Column("disk_write_iops", sa.Float(), nullable=False),
        sa.Column("disk_latency_ms", sa.Float(), nullable=False),
        sa.Column("network_rx_bytes_per_sec", sa.Float(), nullable=False),
        sa.Column("network_tx_bytes_per_sec", sa.Float(), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_host_metric_samples_cluster_host_collected",
        "host_metric_samples",
        ["cluster_id", "host", "collected_at"],
        unique=False,
    )
    op.create_index(
        "ix_host_metric_samples_collected_at",
        "host_metric_samples",
        ["collected_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_host_metric_samples_collected_at", table_name="host_metric_samples")
    op.drop_index("ix_host_metric_samples_cluster_host_collected", table_name="host_metric_samples")
    op.drop_table("host_metric_samples")
