"""add Alert Center acknowledgement and mute state

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
"""

import sqlalchemy as sa
from alembic import op


revision = "d1e2f3a4b5c6"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("acknowledged_at", sa.DateTime(), nullable=True))
    op.add_column("incidents", sa.Column("acknowledged_by", sa.String(length=128), nullable=True))
    op.add_column("incidents", sa.Column("muted_until", sa.DateTime(), nullable=True))
    op.add_column("incidents", sa.Column("muted_by", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_column("muted_by")
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_column("muted_until")
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_column("acknowledged_by")
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_column("acknowledged_at")
