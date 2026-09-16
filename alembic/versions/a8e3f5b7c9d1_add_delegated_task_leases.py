"""add atomic execution leases to delegated AI tasks"""

from alembic import op
import sqlalchemy as sa


revision = "a8e3f5b7c9d1"
down_revision = "a7d2e9f4c1b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("delegated_ai_tasks", sa.Column("execution_owner", sa.String(length=128), nullable=True))
    op.add_column("delegated_ai_tasks", sa.Column("lease_until", sa.DateTime(), nullable=True))
    op.create_index("ix_delegated_ai_tasks_lease_until", "delegated_ai_tasks", ["lease_until"])
    op.add_column("delegated_ai_subtasks", sa.Column("execution_owner", sa.String(length=128), nullable=True))
    op.add_column("delegated_ai_subtasks", sa.Column("lease_until", sa.DateTime(), nullable=True))
    op.create_index("ix_delegated_ai_subtasks_lease_until", "delegated_ai_subtasks", ["lease_until"])


def downgrade() -> None:
    op.drop_index("ix_delegated_ai_subtasks_lease_until", table_name="delegated_ai_subtasks")
    with op.batch_alter_table("delegated_ai_subtasks") as batch_op:
        batch_op.drop_column("lease_until")
        batch_op.drop_column("execution_owner")
    op.drop_index("ix_delegated_ai_tasks_lease_until", table_name="delegated_ai_tasks")
    with op.batch_alter_table("delegated_ai_tasks") as batch_op:
        batch_op.drop_column("lease_until")
        batch_op.drop_column("execution_owner")
