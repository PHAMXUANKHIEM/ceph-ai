"""persist the last successful RGW bucket inventory

Revision ID: c2d3e4f5a6b7
Revises: c0d1e2f3a4b5
"""

import sqlalchemy as sa
from alembic import op


revision = "c2d3e4f5a6b7"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bucket_inventory_snapshots",
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), primary_key=True),
        sa.Column("rgw_host", sa.String(255), nullable=False, server_default=""),
        sa.Column("bucket_names_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("bucket_inventory_snapshots")
