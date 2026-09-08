"""add delegated task dispatch watchdog marker"""

from alembic import op
import sqlalchemy as sa


revision = "a9f4c6d8e0b2"
down_revision = "a8e3f5b7c9d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("delegated_ai_tasks", sa.Column("dispatch_claimed_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_delegated_ai_tasks_dispatch_claimed_at",
        "delegated_ai_tasks",
        ["dispatch_claimed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_delegated_ai_tasks_dispatch_claimed_at", table_name="delegated_ai_tasks")
    op.drop_column("delegated_ai_tasks", "dispatch_claimed_at")
