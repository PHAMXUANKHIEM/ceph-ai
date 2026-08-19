"""create cluster capability inventory

Revision ID: 6b5e22967d5f
Revises: f6a1c3d5e702
Create Date: 2026-08-17
"""

from alembic import op
import sqlalchemy as sa

revision = "6b5e22967d5f"
down_revision = "f6a1c3d5e702"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cluster_capability_inventory",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("deployment_mode", sa.String(16), nullable=True),
        sa.Column("per_type_versions_json", sa.Text(), nullable=True),
        sa.Column("distinct_versions_json", sa.Text(), nullable=True),
        sa.Column("is_mixed_version", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("current_version", sa.String(32), nullable=True),
        sa.Column("current_major", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('SUPPORTED','UNSUPPORTED_VERSION','UNAVAILABLE','UNKNOWN')",
            name="ck_cluster_capability_inventory_status_valid",
        ),
    )
    op.create_index(
        "ix_cluster_capability_inventory_cluster_id",
        "cluster_capability_inventory",
        ["cluster_id"],
    )
    op.create_index(
        "ix_cluster_capability_inventory_collected_at",
        "cluster_capability_inventory",
        ["collected_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cluster_capability_inventory_collected_at", table_name="cluster_capability_inventory")
    op.drop_index("ix_cluster_capability_inventory_cluster_id", table_name="cluster_capability_inventory")
    op.drop_table("cluster_capability_inventory")
