"""store provider-reported token usage for AI cost accounting"""

from alembic import op
import sqlalchemy as sa


revision = "c8d9e0f1a2b3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_invocations", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("ai_invocations", sa.Column("output_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_invocations", "output_tokens")
    op.drop_column("ai_invocations", "input_tokens")
