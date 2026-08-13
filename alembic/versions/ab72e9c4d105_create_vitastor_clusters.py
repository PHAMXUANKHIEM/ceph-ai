"""create independent Vitastor clusters table

Revision ID: ab72e9c4d105
Revises: f3a91c7d2e40
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ab72e9c4d105"
down_revision: Union[str, Sequence[str], None] = "f3a91c7d2e40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vitastor_clusters",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("management_host", sa.String(length=255), nullable=False),
        sa.Column("etcd_address", sa.Text(), nullable=False),
        sa.Column("etcd_prefix", sa.String(length=255), nullable=False),
        sa.Column("config_path", sa.Text(), nullable=False),
        sa.Column("ssh_user", sa.String(length=64), nullable=False),
        sa.Column("ssh_key_path", sa.Text(), nullable=False),
        sa.Column("exec_mode", sa.String(length=16), nullable=False),
        sa.Column("container_name", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_status_json", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_vitastor_clusters_name"),
    )


def downgrade() -> None:
    op.drop_table("vitastor_clusters")
