"""persist node OS gate Prepare rollback markers

Revision ID: 6c8d9e0f1234
Revises: 6b7c8d9e0f12
"""

import sqlalchemy as sa
from alembic import op


revision = "6c8d9e0f1234"
down_revision = "6b7c8d9e0f12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_upgrade_gates") as batch_op:
        batch_op.add_column(sa.Column("maintenance_flags_added", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("mon_removed", sa.Boolean(), nullable=False, server_default=sa.text("false"))
        )


def downgrade() -> None:
    with op.batch_alter_table("node_upgrade_gates") as batch_op:
        batch_op.drop_column("mon_removed")
        batch_op.drop_column("maintenance_flags_added")
