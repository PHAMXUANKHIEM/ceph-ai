"""add backup rpo hours to clusters

Revision ID: f6a1c3d5e702
Revises: e5a7b9c2d401
"""

from alembic import op
import sqlalchemy as sa


revision = "f6a1c3d5e702"
down_revision = "e5a7b9c2d401"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clusters",
        sa.Column("backup_rpo_hours", sa.Integer(), nullable=False, server_default="24"),
    )


def downgrade() -> None:
    op.drop_column("clusters", "backup_rpo_hours")
