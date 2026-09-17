"""add admin-managed Telegram channels and persistent card layout"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b9c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "ad18e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_managed_channels",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("bot_token", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("template", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_telegram_managed_channels_created_at",
        "telegram_managed_channels",
        ["created_at"],
    )
    op.create_table(
        "telegram_channel_layouts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("order_json", sa.Text(), nullable=False),
        sa.Column("hidden_json", sa.Text(), nullable=False),
        sa.Column("display_names_json", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("telegram_channel_layouts")
    op.drop_index("ix_telegram_managed_channels_created_at", table_name="telegram_managed_channels")
    op.drop_table("telegram_managed_channels")
