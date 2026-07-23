"""add severity to incidents

Revision ID: 78524defd561
Revises: b6c957e62a96
Create Date: 2026-07-20 16:50:58.948456

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '78524defd561'
down_revision: Union[str, Sequence[str], None] = 'b6c957e62a96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('incidents', sa.Column('severity', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('incidents', 'severity')
