"""Track the source actor for verified online-learning labels.

Revision ID: b8c9d0e1f2a4
Revises: a7b8c9d0e1f3
"""

from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a4"
down_revision = "a7b8c9d0e1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("online_learner_labels") as batch_op:
        batch_op.add_column(
            sa.Column("source_actor", sa.String(length=64), nullable=False, server_default="forecast-evaluator")
        )


def downgrade() -> None:
    with op.batch_alter_table("online_learner_labels") as batch_op:
        batch_op.drop_column("source_actor")
