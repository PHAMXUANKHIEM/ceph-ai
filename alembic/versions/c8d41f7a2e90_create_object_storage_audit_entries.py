"""create object storage audit entries

Revision ID: c8d41f7a2e90
Revises: a42c9e7d1b30
"""

from alembic import op
import sqlalchemy as sa

revision = "c8d41f7a2e90"
down_revision = "a42c9e7d1b30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "object_storage_audit_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(255), nullable=False),
        sa.Column("preview", sa.Text(), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_object_storage_audit_cluster_time", "object_storage_audit_entries",
        ["cluster_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_object_storage_audit_cluster_time", table_name="object_storage_audit_entries")
    op.drop_table("object_storage_audit_entries")
