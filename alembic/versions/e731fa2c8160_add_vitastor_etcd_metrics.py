"""add Vitastor etcd health metrics

Revision ID: e731fa2c8160
Revises: d942ab17c530
"""
import sqlalchemy as sa
from alembic import op

revision = "e731fa2c8160"
down_revision = "d942ab17c530"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vitastor_metric_samples", sa.Column("etcd_latency_ms", sa.Float(), nullable=True))
    op.add_column("vitastor_metric_samples", sa.Column("etcd_quorum", sa.Boolean(), nullable=True))
    op.add_column("vitastor_metric_samples", sa.Column("etcd_leader_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("vitastor_metric_samples") as batch_op:
        batch_op.drop_column("etcd_leader_count")
    with op.batch_alter_table("vitastor_metric_samples") as batch_op:
        batch_op.drop_column("etcd_quorum")
    with op.batch_alter_table("vitastor_metric_samples") as batch_op:
        batch_op.drop_column("etcd_latency_ms")
