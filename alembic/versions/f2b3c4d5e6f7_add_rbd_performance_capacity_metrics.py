"""add RBD throughput, queue and capacity metric fields

Revision ID: f2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The defaults keep existing VolumeMetric rows readable while allowing
    # the watcher to persist the richer fields from newer rbd_support output.
    with op.batch_alter_table("volume_metrics") as batch_op:
        batch_op.add_column(sa.Column("read_bytes_per_sec", sa.Float(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("write_bytes_per_sec", sa.Float(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("throughput_bytes_per_sec", sa.Float(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("queue_depth", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("used_bytes", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("provisioned_bytes", sa.Integer(), nullable=True))

    # Remove the temporary database default; new rows receive the ORM default
    # and old rows remain explicitly represented as zero/unknown.
    with op.batch_alter_table("volume_metrics") as batch_op:
        batch_op.alter_column("read_bytes_per_sec", server_default=None)
        batch_op.alter_column("write_bytes_per_sec", server_default=None)
        batch_op.alter_column("throughput_bytes_per_sec", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("volume_metrics") as batch_op:
        batch_op.drop_column("provisioned_bytes")
        batch_op.drop_column("used_bytes")
        batch_op.drop_column("queue_depth")
        batch_op.drop_column("throughput_bytes_per_sec")
        batch_op.drop_column("write_bytes_per_sec")
        batch_op.drop_column("read_bytes_per_sec")
