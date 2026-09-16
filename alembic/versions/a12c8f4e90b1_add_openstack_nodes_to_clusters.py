"""add OpenStack controller and compute nodes to clusters

Revision ID: a12c8f4e90b1
Revises: e6c4a8b2d901
"""

from alembic import op
import sqlalchemy as sa


revision = "a12c8f4e90b1"
down_revision = "e6c4a8b2d901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clusters", sa.Column("openstack_controller_nodes", sa.Text(), nullable=False, server_default=""))
    op.add_column("clusters", sa.Column("openstack_compute_nodes", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("clusters") as batch_op:
        batch_op.drop_column("openstack_compute_nodes")
    with op.batch_alter_table("clusters") as batch_op:
        batch_op.drop_column("openstack_controller_nodes")
