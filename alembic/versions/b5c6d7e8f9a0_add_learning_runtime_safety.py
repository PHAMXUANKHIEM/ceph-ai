"""add durable Watcher failure streak for learning safety gate

Revision ID: b5c6d7e8f9a0
Revises: a9b8c7d6e5f4
"""

from alembic import op
import sqlalchemy as sa


revision = "b5c6d7e8f9a0"
down_revision = "d4f7a1c9e2b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watcher_heartbeat",
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "watcher_heartbeat",
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
    )
    # Keep the server default.  It makes this additive migration safe for
    # existing rows and avoids SQLite's unsupported ALTER COLUMN syntax.


def downgrade() -> None:
    # SQLite versions used by the disposable migration gate do not support
    # ALTER TABLE ... DROP COLUMN. Alembic's batch implementation rebuilds
    # the table there and emits native drops on databases that support them.
    with op.batch_alter_table("watcher_heartbeat") as batch_op:
        batch_op.drop_column("last_success_at")
        batch_op.drop_column("consecutive_failures")
