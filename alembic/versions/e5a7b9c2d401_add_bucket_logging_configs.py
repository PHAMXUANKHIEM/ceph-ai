"""add bucket logging configs

Revision ID: e5a7b9c2d401
Revises: d4e6a1b8c203
"""

from alembic import op
import sqlalchemy as sa

revision = "e5a7b9c2d401"
down_revision = "d4e6a1b8c203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bucket_logging_configs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("source_bucket", sa.String(255), nullable=False),
        sa.Column("target_bucket", sa.String(255), nullable=False),
        sa.Column("prefix", sa.String(1024), nullable=False, server_default="logs/"),
        sa.Column("owner", sa.String(255), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("checkpoint", sa.Text()),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_delivery_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("cluster_id", "source_bucket", name="uq_bucket_logging_source"),
    )


def downgrade() -> None:
    op.drop_table("bucket_logging_configs")
