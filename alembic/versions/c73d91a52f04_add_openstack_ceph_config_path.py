"""add OpenStack Ceph config destination path

Revision ID: c73d91a52f04
Revises: a12c8f4e90b1
"""

from alembic import op
import sqlalchemy as sa


revision = "c73d91a52f04"
down_revision = "a12c8f4e90b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clusters",
        sa.Column(
            "openstack_ceph_config_path",
            sa.Text(),
            nullable=False,
            server_default="/etc/ceph",
        ),
    )


def downgrade() -> None:
    op.drop_column("clusters", "openstack_ceph_config_path")
