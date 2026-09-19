"""add promotion blocked reason

Revision ID: e8b9c0d1f234
Revises: d7a8b9c0e123
"""
import sqlalchemy as sa
from alembic import op

revision = "e8b9c0d1f234"
down_revision = "d7a8b9c0e123"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("playbook_stats", sa.Column("promotion_blocked_reason", sa.Text()))


def downgrade():
    with op.batch_alter_table("playbook_stats") as batch_op:
        batch_op.drop_column("promotion_blocked_reason")
