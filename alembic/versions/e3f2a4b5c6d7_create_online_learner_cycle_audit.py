"""bounded online learner cycle resource telemetry

Revision ID: e3f4a5b6c7d8
Revises: d8e9f0a1b2c3
"""

from alembic import op
import sqlalchemy as sa


revision = "e3f4a5b6c7d8"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "online_learner_cycle_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_key", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("applied", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("elapsed_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cpu_time_ms", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("runtime_mode", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_online_learner_cycle_audit_created_at",
        "online_learner_cycle_audit", ["created_at"], unique=False,
    )
    op.create_index(
        "ix_online_learner_cycle_audit_scope_time",
        "online_learner_cycle_audit",
        ["cluster_key", "host", "metric", "created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_online_learner_cycle_audit_scope_time",
        table_name="online_learner_cycle_audit",
    )
    op.drop_index(
        "ix_online_learner_cycle_audit_created_at",
        table_name="online_learner_cycle_audit",
    )
    op.drop_table("online_learner_cycle_audit")
