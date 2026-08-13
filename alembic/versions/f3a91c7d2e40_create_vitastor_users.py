"""create independent Vitastor users table

Revision ID: f3a91c7d2e40
Revises: 82c4d1e9a6f0
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a91c7d2e40"
down_revision: Union[str, Sequence[str], None] = "82c4d1e9a6f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vitastor_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=72), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_vitastor_users_username"),
    )


def downgrade() -> None:
    op.drop_table("vitastor_users")
