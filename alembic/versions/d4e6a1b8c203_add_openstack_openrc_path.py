"""add OpenStack openrc path

Revision ID: d4e6a1b8c203
Revises: c8d41f7a2e90
"""

from alembic import op
import sqlalchemy as sa


revision = "d4e6a1b8c203"
down_revision = "c8d41f7a2e90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clusters", sa.Column("openstack_openrc_path", sa.Text(), nullable=False, server_default="")
    )


def downgrade() -> None:
    op.drop_column("clusters", "openstack_openrc_path")
