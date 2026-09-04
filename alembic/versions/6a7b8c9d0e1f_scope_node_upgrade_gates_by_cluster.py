"""scope node OS upgrade gates by cluster

Revision ID: 6a7b8c9d0e1f
Revises: 5f6a7b8c9d03
"""

import sqlalchemy as sa
from alembic import op


revision = "6a7b8c9d0e1f"
down_revision = "5f6a7b8c9d03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_upgrade_gates") as batch_op:
        batch_op.add_column(sa.Column("cluster_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_node_upgrade_gates_cluster_id", "clusters", ["cluster_id"], ["id"]
        )
        batch_op.create_index("ix_node_upgrade_gates_cluster_id", ["cluster_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("node_upgrade_gates") as batch_op:
        batch_op.drop_index("ix_node_upgrade_gates_cluster_id")
        batch_op.drop_constraint("fk_node_upgrade_gates_cluster_id", type_="foreignkey")
        batch_op.drop_column("cluster_id")
