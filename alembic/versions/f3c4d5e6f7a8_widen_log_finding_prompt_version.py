"""widen LogFinding prompt version for versioned analysis prompts

Revision ID: f3c4d5e6f7a8
Revises: f2c3d4e5f6a7
"""

from alembic import op
import sqlalchemy as sa


revision = "f3c4d5e6f7a8"
down_revision = "f2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("log_findings") as batch_op:
        batch_op.alter_column(
            "prompt_version",
            existing_type=sa.String(length=16),
            type_=sa.String(length=64),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("log_findings") as batch_op:
        batch_op.alter_column(
            "prompt_version",
            existing_type=sa.String(length=64),
            type_=sa.String(length=16),
            existing_nullable=True,
        )
