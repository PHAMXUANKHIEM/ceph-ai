"""create Vitastor cluster and OSD metric history

Revision ID: d942ab17c530
Revises: c821d39a7f10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d942ab17c530"
down_revision: Union[str, Sequence[str], None] = "c821d39a7f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_metric_samples",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("health", sa.String(16), nullable=False),
        sa.Column("osd_up", sa.Integer(), nullable=False), sa.Column("osd_total", sa.Integer(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False), sa.Column("free_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_percent", sa.Float(), nullable=False),
        sa.Column("etcd_up", sa.Integer(), nullable=False), sa.Column("etcd_total", sa.Integer(), nullable=False),
        sa.Column("read_iops", sa.Float(), nullable=False), sa.Column("write_iops", sa.Float(), nullable=False),
        sa.Column("read_bps", sa.Float(), nullable=False), sa.Column("write_bps", sa.Float(), nullable=False),
        sa.Column("read_latency_ms", sa.Float(), nullable=True), sa.Column("write_latency_ms", sa.Float(), nullable=True),
        sa.Column("recovery_bps", sa.Float(), nullable=False), sa.Column("degraded_bytes", sa.BigInteger(), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=False), sa.Column("collected_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vitastor_metric_cluster_time", "vitastor_metric_samples", ["cluster_id", "collected_at"])
    op.create_table(
        "vitastor_osd_metric_samples",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cluster_id", sa.String(36), nullable=False), sa.Column("osd_id", sa.String(64), nullable=False),
        sa.Column("host", sa.String(255), nullable=False), sa.Column("is_up", sa.Boolean(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False), sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_percent", sa.Float(), nullable=False),
        sa.Column("read_iops", sa.Float(), nullable=False), sa.Column("write_iops", sa.Float(), nullable=False),
        sa.Column("read_bps", sa.Float(), nullable=False), sa.Column("write_bps", sa.Float(), nullable=False),
        sa.Column("read_latency_ms", sa.Float(), nullable=True), sa.Column("write_latency_ms", sa.Float(), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=False), sa.Column("collected_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vitastor_osd_metric_cluster_osd_time", "vitastor_osd_metric_samples", ["cluster_id", "osd_id", "collected_at"])


def downgrade() -> None:
    op.drop_index("ix_vitastor_osd_metric_cluster_osd_time", table_name="vitastor_osd_metric_samples")
    op.drop_table("vitastor_osd_metric_samples")
    op.drop_index("ix_vitastor_metric_cluster_time", table_name="vitastor_metric_samples")
    op.drop_table("vitastor_metric_samples")
