"""Persist delegated provider-call budget across worker reclaim.

Revision ID: aa05c7e9f1d3
Revises: a9f4c6d8e0b2
"""

from alembic import op
import sqlalchemy as sa


revision = "aa05c7e9f1d3"
down_revision = "a9f4c6d8e0b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "delegated_ai_tasks",
        sa.Column("provider_call_count", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    with op.batch_alter_table("delegated_ai_tasks") as batch_op:
        batch_op.drop_column("provider_call_count")
