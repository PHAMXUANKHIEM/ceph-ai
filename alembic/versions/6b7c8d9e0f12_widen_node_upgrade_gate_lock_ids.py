"""allow per-cluster node OS gate lock identifiers

Revision ID: 6b7c8d9e0f12
Revises: 6a7b8c9d0e1f
"""

import sqlalchemy as sa
from alembic import op


revision = "6b7c8d9e0f12"
down_revision = "6a7b8c9d0e1f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_upgrade_gate_locks") as batch_op:
        batch_op.alter_column(
            "id",
            existing_type=sa.String(length=32),
            type_=sa.String(length=64),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("node_upgrade_gate_locks") as batch_op:
        batch_op.alter_column(
            "id",
            existing_type=sa.String(length=64),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
