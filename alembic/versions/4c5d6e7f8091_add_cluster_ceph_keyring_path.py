"""add required Ceph keyring path to clusters

Revision ID: 4c5d6e7f8091
Revises: 7a8b9c0d1e22
"""

from alembic import op
import sqlalchemy as sa

revision = "4c5d6e7f8091"
down_revision = "7a8b9c0d1e22"
branch_labels = None
depends_on = None

DEFAULT_KEYRING = "/etc/ceph/ceph.client.admin.keyring"


def upgrade() -> None:
    with op.batch_alter_table("clusters") as batch_op:
        batch_op.add_column(sa.Column(
            "ceph_keyring_path", sa.Text(), nullable=False, server_default=DEFAULT_KEYRING
        ))


def downgrade() -> None:
    with op.batch_alter_table("clusters") as batch_op:
        batch_op.drop_column("ceph_keyring_path")
