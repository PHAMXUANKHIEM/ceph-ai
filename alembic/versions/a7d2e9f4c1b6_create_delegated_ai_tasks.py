"""create delegated AI supervisor task tables"""
from alembic import op
import sqlalchemy as sa

revision = "a7d2e9f4c1b6"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delegated_ai_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("assistant_message_id", sa.String(length=36), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["chat_messages.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_delegated_ai_tasks_actor", "delegated_ai_tasks", ["actor"])
    op.create_index("ix_delegated_ai_tasks_cluster_id", "delegated_ai_tasks", ["cluster_id"])
    op.create_index("ix_delegated_ai_tasks_session_id", "delegated_ai_tasks", ["session_id"])
    op.create_index("ix_delegated_ai_tasks_status", "delegated_ai_tasks", ["status"])
    op.create_index("ix_delegated_ai_tasks_actor_created", "delegated_ai_tasks", ["actor", "created_at"])
    op.create_index("ix_delegated_ai_tasks_status_updated", "delegated_ai_tasks", ["status", "updated_at"])

    op.create_table(
        "delegated_ai_subtasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=48), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("allowed_tools_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("tools_used_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["delegated_ai_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "role", name="uq_delegated_ai_subtask_task_role"),
    )
    op.create_index("ix_delegated_ai_subtasks_task_id", "delegated_ai_subtasks", ["task_id"])
    op.create_index("ix_delegated_ai_subtasks_status", "delegated_ai_subtasks", ["status"])
    op.create_index("ix_delegated_ai_subtasks_task_status", "delegated_ai_subtasks", ["task_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_delegated_ai_subtasks_task_status", table_name="delegated_ai_subtasks")
    op.drop_index("ix_delegated_ai_subtasks_status", table_name="delegated_ai_subtasks")
    op.drop_index("ix_delegated_ai_subtasks_task_id", table_name="delegated_ai_subtasks")
    op.drop_table("delegated_ai_subtasks")
    op.drop_index("ix_delegated_ai_tasks_status_updated", table_name="delegated_ai_tasks")
    op.drop_index("ix_delegated_ai_tasks_actor_created", table_name="delegated_ai_tasks")
    op.drop_index("ix_delegated_ai_tasks_status", table_name="delegated_ai_tasks")
    op.drop_index("ix_delegated_ai_tasks_session_id", table_name="delegated_ai_tasks")
    op.drop_index("ix_delegated_ai_tasks_cluster_id", table_name="delegated_ai_tasks")
    op.drop_index("ix_delegated_ai_tasks_actor", table_name="delegated_ai_tasks")
    op.drop_table("delegated_ai_tasks")
