"""create per-user chat preferences

Revision ID: e91b6a4c2d10
Revises: d84e2f0a7b31
"""

from alembic import op
import sqlalchemy as sa


revision = "e91b6a4c2d10"
down_revision = "d84e2f0a7b31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_preferences",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("ai_name", sa.String(length=64), nullable=False, server_default="AI"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("username"),
    )


def downgrade() -> None:
    op.drop_table("chat_preferences")
