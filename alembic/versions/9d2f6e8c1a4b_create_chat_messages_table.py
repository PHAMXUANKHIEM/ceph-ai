"""create chat_messages table

Revision ID: 9d2f6e8c1a4b
Revises: 78524defd561
Create Date: 2026-07-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d2f6e8c1a4b'
down_revision: Union[str, Sequence[str], None] = '78524defd561'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('chat_messages',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('actor', sa.String(length=32), nullable=True),
    sa.Column('proposed_action_id', sa.String(length=64), nullable=True),
    sa.Column('proposed_target_nodes', sa.Text(), nullable=True),
    sa.Column('proposed_rationale', sa.Text(), nullable=True),
    sa.Column('proposed_command_preview', sa.Text(), nullable=True),
    sa.Column('proposed_status', sa.String(length=16), nullable=True),
    sa.Column('proposed_incident_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['proposed_incident_id'], ['incidents.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('chat_messages')
