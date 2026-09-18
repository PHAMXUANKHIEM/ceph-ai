"""create append-only online learner audit records

Revision ID: e4f5a6b7c8d9
Revises: c1d2e3f4a5b7
"""

from alembic import op
import sqlalchemy as sa


revision = "e4f5a6b7c8d9"
down_revision = "c1d2e3f4a5b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "online_learner_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_key", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("sample_id", sa.String(length=255), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("label", sa.Float(), nullable=True),
        sa.Column("quality_status", sa.String(length=32), nullable=False),
        sa.Column("quality_reason", sa.Text(), nullable=False),
        sa.Column("runtime_mode", sa.String(length=24), nullable=False),
        sa.Column("runtime_reason", sa.Text(), nullable=False),
        sa.Column("update_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cluster_key", "host", "metric", "sample_id",
            name="uq_online_learner_audit_sample",
        ),
    )
    op.create_index(
        "ix_online_learner_audit_stream_time",
        "online_learner_audit", ["cluster_key", "host", "metric", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_online_learner_audit_stream_time", table_name="online_learner_audit")
    op.drop_table("online_learner_audit")
