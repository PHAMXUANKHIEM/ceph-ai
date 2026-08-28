"""create persistent cache for validated AI runbooks

Revision ID: c3d4e5f6a789
Revises: b8c9d0e1f2a3
"""

import sqlalchemy as sa
from alembic import op


revision = "c3d4e5f6a789"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_runbooks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("cluster_id", sa.String(length=36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("fault_family", sa.String(length=64), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("source_case_count", sa.Integer(), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "uq_ai_runbooks_source",
        "ai_runbooks",
        ["cluster_id", "fault_family", "source_fingerprint", "prompt_version"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_ai_runbooks_source", table_name="ai_runbooks")
    op.drop_table("ai_runbooks")
