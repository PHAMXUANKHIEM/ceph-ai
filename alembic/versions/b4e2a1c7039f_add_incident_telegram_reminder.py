"""add incident telegram reminder timestamp

Revision ID: b4e2a1c7039f
Revises: ac91e4d87210
Create Date: 2026-08-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4e2a1c7039f"
down_revision: Union[str, Sequence[str], None] = "ac91e4d87210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("telegram_reminded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("incidents", "telegram_reminded_at")
