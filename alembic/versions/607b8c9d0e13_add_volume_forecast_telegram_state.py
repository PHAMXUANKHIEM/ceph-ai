"""add RBD forecast Telegram delivery state

Revision ID: 607b8c9d0e13
Revises: 5f6a7b8c9d02
"""

from alembic import op
import sqlalchemy as sa

revision = "607b8c9d0e13"
down_revision = "5f6a7b8c9d02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("volume_early_forecasts", sa.Column("telegram_sent_at", sa.DateTime(), nullable=True))
    op.create_index("ix_volume_early_forecasts_telegram_sent_at", "volume_early_forecasts", ["telegram_sent_at"])


def downgrade() -> None:
    op.drop_index("ix_volume_early_forecasts_telegram_sent_at", table_name="volume_early_forecasts")
    op.drop_column("volume_early_forecasts", "telegram_sent_at")
