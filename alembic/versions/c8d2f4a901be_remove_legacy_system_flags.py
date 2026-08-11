"""remove obsolete system flags storage

Revision ID: c8d2f4a901be
Revises: b7e39a2c410f
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8d2f4a901be"
down_revision: Union[str, Sequence[str], None] = "b7e39a2c410f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("system_flags")


def downgrade() -> None:
    op.create_table(
        "system_flags",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
