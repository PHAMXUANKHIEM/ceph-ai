"""add cluster_id to volume_metrics

Revision ID: e6c4a8b2d901
Revises: c8d2f4a901be
Create Date: 2026-08-12 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6c4a8b2d901"
down_revision: Union[str, Sequence[str], None] = "c8d2f4a901be"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("volume_metrics", sa.Column("cluster_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("volume_metrics") as batch_op:
        batch_op.create_foreign_key(
            "fk_volume_metrics_cluster_id", "clusters", ["cluster_id"], ["id"]
        )
    op.create_index("ix_volume_metrics_cluster_id", "volume_metrics", ["cluster_id"])


def downgrade() -> None:
    op.drop_index("ix_volume_metrics_cluster_id", table_name="volume_metrics")
    with op.batch_alter_table("volume_metrics") as batch_op:
        batch_op.drop_constraint("fk_volume_metrics_cluster_id", type_="foreignkey")
    op.drop_column("volume_metrics", "cluster_id")
