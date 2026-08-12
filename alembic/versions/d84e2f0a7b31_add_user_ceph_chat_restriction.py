"""add per-user Ceph chat restriction

Revision ID: d84e2f0a7b31
Revises: c73d91a52f04
"""

from alembic import op
import sqlalchemy as sa


revision = "d84e2f0a7b31"
down_revision = "c73d91a52f04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ceph_chat_restricted", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("users", "ceph_chat_restricted")
