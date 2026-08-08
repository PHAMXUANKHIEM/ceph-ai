"""create crush structure snapshots table

Revision ID: 85650f5c02f3
Revises: 6b1f3a9d7e2c
Create Date: 2026-08-07 14:58:46.715142

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '85650f5c02f3'
down_revision: Union[str, Sequence[str], None] = '6b1f3a9d7e2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'crush_structure_snapshots',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tree_json', sa.Text(), nullable=False),
        sa.Column('diff_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('crush_structure_snapshots')
