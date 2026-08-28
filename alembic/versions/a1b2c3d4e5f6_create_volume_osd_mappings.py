"""create latest RBD volume to PG/OSD mappings

Revision ID: a1b2c3d4e5f6
Revises: e5f6a7b8c9d0
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "volume_osd_mappings",
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("pool", sa.String(length=64), nullable=False),
        sa.Column("image", sa.String(length=128), nullable=False),
        sa.Column("image_id", sa.String(length=128), nullable=False),
        sa.Column("object_name", sa.String(length=255), nullable=False),
        sa.Column("pgid", sa.String(length=64), nullable=False),
        sa.Column("acting_osds_json", sa.Text(), nullable=False),
        sa.Column("primary_osd", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("cluster_id", "pool", "image"),
    )
    op.create_index(
        "ix_volume_osd_mappings_cluster_captured",
        "volume_osd_mappings",
        ["cluster_id", "captured_at"],
        unique=False,
    )
    op.create_index(
        "ix_volume_osd_mappings_captured_at",
        "volume_osd_mappings",
        ["captured_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_volume_osd_mappings_captured_at", table_name="volume_osd_mappings")
    op.drop_index("ix_volume_osd_mappings_cluster_captured", table_name="volume_osd_mappings")
    op.drop_table("volume_osd_mappings")
