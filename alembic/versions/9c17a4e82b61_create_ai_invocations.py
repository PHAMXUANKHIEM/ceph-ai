"""create content-free AI invocation telemetry

Revision ID: 9c17a4e82b61
Revises: 718c9d0e1f24
"""
from alembic import op
import sqlalchemy as sa

revision = "9c17a4e82b61"
down_revision = "718c9d0e1f24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_invocations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("feature", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_chars", sa.Integer(), nullable=False),
        sa.Column("output_chars", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_invocations_created_at", "ai_invocations", ["created_at"])
    op.create_index("ix_ai_invocations_feature_status", "ai_invocations", ["feature", "status"])


def downgrade() -> None:
    op.drop_index("ix_ai_invocations_feature_status", table_name="ai_invocations")
    op.drop_index("ix_ai_invocations_created_at", table_name="ai_invocations")
    op.drop_table("ai_invocations")
