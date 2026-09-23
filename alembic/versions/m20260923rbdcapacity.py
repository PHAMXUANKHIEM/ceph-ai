"""Persist bounded RBD volume and snapshot capacity observations."""

from alembic import op
import sqlalchemy as sa

revision = "m20260923rbdcapacity"
down_revision = "m20260923incidentmetrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rbd_capacity_samples",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("cluster_id", sa.String(length=36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("pool", sa.String(length=128), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_count", sa.Integer(), nullable=False),
        sa.Column("provisioned_bytes", sa.BigInteger(), nullable=False),
        sa.Column("head_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("physical_pool_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_rbd_capacity_series", "rbd_capacity_samples",
        ["cluster_id", "pool", "captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rbd_capacity_series", table_name="rbd_capacity_samples")
    op.drop_table("rbd_capacity_samples")
