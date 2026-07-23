"""add tools_used to chat_messages

Revision ID: 2f4a8c1e6d3b
Revises: 9d2f6e8c1a4b
Create Date: 2026-07-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f4a8c1e6d3b'
down_revision: Union[str, Sequence[str], None] = '9d2f6e8c1a4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('chat_messages', sa.Column('tools_used', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chat_messages', 'tools_used')
