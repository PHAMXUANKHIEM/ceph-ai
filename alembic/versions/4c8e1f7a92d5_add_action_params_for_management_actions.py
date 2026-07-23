"""add action_params for management actions

Revision ID: 4c8e1f7a92d5
Revises: 7a1c9e5f2b4d
Create Date: 2026-07-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4c8e1f7a92d5'
down_revision: Union[str, Sequence[str], None] = '7a1c9e5f2b4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('actions', sa.Column('action_params', sa.Text(), nullable=True))
    op.add_column('chat_messages', sa.Column('proposed_action_params', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chat_messages', 'proposed_action_params')
    op.drop_column('actions', 'action_params')
