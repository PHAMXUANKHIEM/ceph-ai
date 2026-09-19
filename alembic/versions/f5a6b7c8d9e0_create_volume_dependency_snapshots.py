"""persist bounded RBD snapshot/clone dependency observations"""

from alembic import op
import sqlalchemy as sa


revision = "f5a6b7c8d9e0"
down_revision = "f4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "volume_dependency_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("pool", sa.String(length=64), nullable=False),
        sa.Column("image", sa.String(length=128), nullable=False),
        sa.Column("snapshot_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parent_json", sa.Text(), nullable=True),
        sa.Column("children_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("partial_errors_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_volume_dependency_snapshots_scope",
        "volume_dependency_snapshots",
        ["cluster_id", "pool", "image", "captured_at"],
    )
    op.create_index(
        "ix_volume_dependency_snapshots_captured",
        "volume_dependency_snapshots",
        ["captured_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_volume_dependency_snapshots_captured", table_name="volume_dependency_snapshots")
    op.drop_index("ix_volume_dependency_snapshots_scope", table_name="volume_dependency_snapshots")
    op.drop_table("volume_dependency_snapshots")
