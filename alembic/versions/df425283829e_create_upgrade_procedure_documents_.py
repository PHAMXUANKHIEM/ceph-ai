"""create upgrade_procedure_documents table

Revision ID: df425283829e
Revises: 4c8e1f7a92d5
Create Date: 2026-07-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'df425283829e'
down_revision: Union[str, Sequence[str], None] = '4c8e1f7a92d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('upgrade_procedure_documents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('raw_text', sa.Text(), nullable=False),
    sa.Column('summary_text', sa.Text(), nullable=True),
    sa.Column('summary_error', sa.Text(), nullable=True),
    sa.Column('uploaded_by', sa.String(length=32), nullable=False),
    sa.Column('uploaded_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('upgrade_procedure_documents')
