"""split telegram_message_id into per-channel telegram_message_ids

Revision ID: 6b1f3a9d7e2c
Revises: 91d7e9723457
Create Date: 2026-08-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '6b1f3a9d7e2c'
down_revision: Union[str, Sequence[str], None] = '91d7e9723457'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('actions', sa.Column('telegram_message_ids', sa.Text(), nullable=True))
    op.drop_column('actions', 'telegram_message_id')


def downgrade() -> None:
    op.add_column('actions', sa.Column('telegram_message_id', sa.Integer(), nullable=True))
    op.drop_column('actions', 'telegram_message_ids')
