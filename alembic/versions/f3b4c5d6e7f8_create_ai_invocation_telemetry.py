"""create content-free AI invocation telemetry

Revision ID: f3b4c5d6e7f8
Revises: f2b3c4d5e6f7
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "f2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_invocations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("feature", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False, server_default="unknown"),
        sa.Column("status", sa.String(16), nullable=False, server_default="RESERVED"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_estimated", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=True),
        sa.Column("actor", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_ai_invocations_created_feature", "ai_invocations", ["created_at", "feature"]
    )
    op.create_index(
        "ix_ai_invocations_created_status", "ai_invocations", ["created_at", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_invocations_created_status", table_name="ai_invocations")
    op.drop_index("ix_ai_invocations_created_feature", table_name="ai_invocations")
    op.drop_table("ai_invocations")
