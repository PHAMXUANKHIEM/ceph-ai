"""add append-only Ceph capacity samples

Revision ID: 8a1b2c3d4e5f
Revises: 7f8091a2b3c4
"""
from alembic import op
import sqlalchemy as sa

revision = "8a1b2c3d4e5f"
down_revision = "7f8091a2b3c4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ceph_capacity_samples",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_name", sa.String(128), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_percent", sa.Float(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ceph_capacity_series", "ceph_capacity_samples", ["cluster_id", "entity_type", "entity_name", "captured_at"])


def downgrade():
    op.drop_index("ix_ceph_capacity_series", table_name="ceph_capacity_samples")
    op.drop_table("ceph_capacity_samples")
