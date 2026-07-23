"""create node_diagnostic_runs table

Revision ID: b6c957e62a96
Revises: 3859af1d019a
Create Date: 2026-07-20 16:04:10.330916

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6c957e62a96'
down_revision: Union[str, Sequence[str], None] = '3859af1d019a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('node_diagnostic_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('host', sa.String(length=64), nullable=False),
    sa.Column('command_id', sa.String(length=64), nullable=False),
    sa.Column('command_label', sa.String(length=128), nullable=False),
    sa.Column('actor', sa.String(length=32), nullable=False),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('output_excerpt', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('node_diagnostic_runs')
