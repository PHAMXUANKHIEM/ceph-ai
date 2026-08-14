"""add configurable feminine chat address

Revision ID: c7d4e5f6a701
Revises: b4e2a1c7039f
"""

from alembic import op
import sqlalchemy as sa


revision: str = "c7d4e5f6a701"
down_revision: str = "b4e2a1c7039f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_preferences",
        sa.Column(
            "female_address",
            sa.String(length=128),
            nullable=False,
            server_default="Mình yêu ơi, em là",
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_preferences", "female_address")
