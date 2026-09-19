"""Persist safe natural-language context for chat messages.

Revision ID: m20260919nlcontext
Revises: m20260919forecastevents
"""

from alembic import op
import sqlalchemy as sa


revision = "m20260919nlcontext"
down_revision = "m20260919forecastevents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("nl_context_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("chat_messages") as batch_op:
        batch_op.drop_column("nl_context_json")
