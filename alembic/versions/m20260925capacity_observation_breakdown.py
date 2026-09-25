"""Add capacity accounting, attribution and alert lifecycle fields."""

from alembic import op
import sqlalchemy as sa


revision = "m20260925capacity"
down_revision = "m20260921backupalert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ceph_capacity_samples") as batch:
        batch.add_column(sa.Column("provisioned_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("logical_used_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("snapshot_provisioned_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("snapshot_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("snapshot_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("replica_factor", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("ec_k", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("ec_m", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("redundancy_overhead_bytes", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("failure_domain_reserve_percent", sa.Float(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("quality_status", sa.String(length=32), nullable=False, server_default="OK"))
    with op.batch_alter_table("capacity_alert_states") as batch:
        batch.add_column(sa.Column("status", sa.String(length=16), nullable=False, server_default="RESOLVED"))
        batch.add_column(sa.Column("opened_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("resolved_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("last_notified_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("notification_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("capacity_alert_states") as batch:
        for name in ("notification_count", "last_notified_at", "resolved_at", "opened_at", "status"):
            batch.drop_column(name)
    with op.batch_alter_table("ceph_capacity_samples") as batch:
        for name in (
            "quality_status", "failure_domain_reserve_percent", "redundancy_overhead_bytes",
            "ec_m", "ec_k", "replica_factor", "snapshot_count", "snapshot_bytes",
            "snapshot_provisioned_bytes",
            "logical_used_bytes", "provisioned_bytes",
        ):
            batch.drop_column(name)
