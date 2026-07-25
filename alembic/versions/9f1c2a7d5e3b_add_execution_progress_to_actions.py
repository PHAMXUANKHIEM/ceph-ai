"""add execution_progress to actions

Revision ID: 9f1c2a7d5e3b
Revises: 02ccb9ea637d
Create Date: 2026-07-24 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9f1c2a7d5e3b'
down_revision: Union[str, Sequence[str], None] = '02ccb9ea637d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('actions', sa.Column('execution_progress', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('actions', 'execution_progress')
