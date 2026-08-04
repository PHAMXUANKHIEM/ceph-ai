"""add client_host to test_runner_configs

Revision ID: ddd4ddeaa094
Revises: f5f9c4cd268f
Create Date: 2026-08-04 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'ddd4ddeaa094'
down_revision: Union[str, Sequence[str], None] = 'f5f9c4cd268f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('test_runner_configs', sa.Column('client_host', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('test_runner_configs', 'client_host')
