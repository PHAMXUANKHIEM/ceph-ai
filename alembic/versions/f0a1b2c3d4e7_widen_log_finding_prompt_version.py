"""widen LogFinding prompt version for versioned analysis prompts

Revision ID: f0a1b2c3d4e7
Revises: f0a1b2c3d4e6
"""

from alembic import op
import sqlalchemy as sa


revision = "f0a1b2c3d4e7"
down_revision = "f0a1b2c3d4e6"
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
