"""add telegram approval fields to actions

Revision ID: 38df461ab6d0
Revises: ddd4ddeaa094
Create Date: 2026-08-05 09:16:39.777988

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '38df461ab6d0'
down_revision: Union[str, Sequence[str], None] = 'ddd4ddeaa094'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('actions', sa.Column('telegram_message_id', sa.Integer(), nullable=True))
    op.add_column('actions', sa.Column('telegram_notified_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('actions', 'telegram_notified_at')
    op.drop_column('actions', 'telegram_message_id')
