"""Add outcome policy metadata to verified online-learning labels.

Revision ID: a7b8c9d0e1f3
Revises: f6a7b8c9d0e1
"""

from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f3"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("online_learner_labels") as batch_op:
        batch_op.add_column(sa.Column("predicted_value", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("absolute_error", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column("outcome", sa.String(length=24), nullable=False, server_default="VERIFIED_SUCCESS")
        )
        batch_op.add_column(
            sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="1")
        )


def downgrade() -> None:
    with op.batch_alter_table("online_learner_labels") as batch_op:
        batch_op.drop_column("evidence_count")
        batch_op.drop_column("outcome")
        batch_op.drop_column("absolute_error")
        batch_op.drop_column("predicted_value")
