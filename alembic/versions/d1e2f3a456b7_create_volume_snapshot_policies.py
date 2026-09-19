"""create per-volume snapshot policies

Revision ID: d1e2f3a456b7
Revises: c0d1e2f3a456
"""

from alembic import op
import sqlalchemy as sa


revision = "d1e2f3a456b7"
down_revision = "c0d1e2f3a456"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "volume_snapshot_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=True),
        sa.Column("pool", sa.String(length=64), nullable=False),
        sa.Column("image", sa.String(length=128), nullable=False),
        sa.Column("volume_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_prefix", sa.String(length=48), nullable=False),
        sa.Column("cron_expression", sa.String(length=128), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("retention_count", sa.Integer(), nullable=False),
        sa.Column("capacity_guard_percent", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=24), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "retention_count >= 1 AND retention_count <= 365",
            name="ck_volume_snapshot_policy_retention",
        ),
        sa.CheckConstraint(
            "capacity_guard_percent > 0 AND capacity_guard_percent < 100",
            name="ck_volume_snapshot_policy_capacity_guard",
        ),
    )
    op.create_index(
        "ix_volume_snapshot_policies_cluster_enabled",
        "volume_snapshot_policies", ["cluster_id", "enabled"],
    )
    op.create_index(
        "ix_volume_snapshot_policies_next_run",
        "volume_snapshot_policies", ["next_run_at"],
    )
    # Nullable cluster_id means the legacy/default cluster. A normal UNIQUE
    # constraint treats NULLs as distinct, so use the same COALESCE scope as
    # the application's other default-cluster guards.
    op.execute(
        "CREATE UNIQUE INDEX uq_volume_snapshot_policy_target "
        "ON volume_snapshot_policies (COALESCE(cluster_id, ''), pool, image)"
    )


def downgrade() -> None:
    op.drop_index("uq_volume_snapshot_policy_target", table_name="volume_snapshot_policies")
    op.drop_index("ix_volume_snapshot_policies_next_run", table_name="volume_snapshot_policies")
    op.drop_index("ix_volume_snapshot_policies_cluster_enabled", table_name="volume_snapshot_policies")
    op.drop_table("volume_snapshot_policies")
