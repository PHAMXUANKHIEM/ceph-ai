"""widen LogFinding verdict for INSUFFICIENT_EVIDENCE

Revision ID: ac17d9e0f5a0
Revises: ab16c8d9e0f4
"""

from alembic import op
import sqlalchemy as sa


revision = "ac17d9e0f5a0"
down_revision = "ab16c8d9e0f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "log_findings",
        "verdict",
        existing_type=sa.String(length=16),
        type_=sa.String(length=24),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "log_findings",
        "verdict",
        existing_type=sa.String(length=24),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
