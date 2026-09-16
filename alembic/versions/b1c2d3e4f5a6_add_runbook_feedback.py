"""add operator feedback fields to cached AI runbooks"""

from alembic import op
import sqlalchemy as sa


revision = "b1c2d3e4f5a6"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_runbooks", sa.Column("feedback_rating", sa.String(length=16), nullable=True))
    op.add_column("ai_runbooks", sa.Column("feedback_note", sa.Text(), nullable=True))
    op.add_column("ai_runbooks", sa.Column("feedback_by", sa.String(length=128), nullable=True))
    op.add_column("ai_runbooks", sa.Column("feedback_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ai_runbooks") as batch_op:
        batch_op.drop_column("feedback_at")
    with op.batch_alter_table("ai_runbooks") as batch_op:
        batch_op.drop_column("feedback_by")
    with op.batch_alter_table("ai_runbooks") as batch_op:
        batch_op.drop_column("feedback_note")
    with op.batch_alter_table("ai_runbooks") as batch_op:
        batch_op.drop_column("feedback_rating")
