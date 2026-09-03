"""widen backup job byte counts

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
"""

import sqlalchemy as sa
from alembic import op


revision = "3c4d5e6f7a8b"
down_revision = "2b3c4d5e6f7a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "backup_jobs",
        "size_bytes",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "backup_jobs",
        "size_bytes",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
