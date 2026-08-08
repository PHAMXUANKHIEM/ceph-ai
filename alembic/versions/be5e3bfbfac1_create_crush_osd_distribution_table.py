"""create crush osd distribution table

Revision ID: be5e3bfbfac1
Revises: 85650f5c02f3
Create Date: 2026-08-07 14:59:10.335284

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'be5e3bfbfac1'
down_revision: Union[str, Sequence[str], None] = '85650f5c02f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'crush_osd_distribution',
        sa.Column('osd_id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('host', sa.String(length=64), nullable=True),
        sa.Column('bytes_used', sa.BigInteger(), nullable=True),
        sa.Column('bytes_total', sa.BigInteger(), nullable=True),
        sa.Column('pgs', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('osd_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('crush_osd_distribution')
