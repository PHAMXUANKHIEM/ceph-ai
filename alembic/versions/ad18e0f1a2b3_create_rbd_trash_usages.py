"""create durable RBD Trash usage snapshots

Revision ID: ad18e0f1a2b3
Revises: ac17d9e0f5a0
"""

from alembic import op
import sqlalchemy as sa


revision = "ad18e0f1a2b3"
down_revision = "ac17d9e0f5a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rbd_trash_usages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("pool", sa.String(length=64), nullable=False),
        sa.Column("trash_id", sa.String(length=128), nullable=False),
        sa.Column("image", sa.String(length=128), nullable=False),
        sa.Column("provisioned_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_percent", sa.Float(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cluster_id", "pool", "trash_id", name="uq_rbd_trash_usages_cluster_pool_id"
        ),
    )
    op.create_index(
        "ix_rbd_trash_usages_cluster_pool",
        "rbd_trash_usages",
        ["cluster_id", "pool"],
    )


def downgrade() -> None:
    op.drop_index("ix_rbd_trash_usages_cluster_pool", table_name="rbd_trash_usages")
    op.drop_table("rbd_trash_usages")
