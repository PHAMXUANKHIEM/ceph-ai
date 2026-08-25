"""add audited operator verdicts to daemon-log learning

Revision ID: 3d4e5f6a7b80
Revises: 2c3d4e5f6a79
"""
from alembic import op
import sqlalchemy as sa

revision = "3d4e5f6a7b80"
down_revision = "2c3d4e5f6a79"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("log_learning_samples", sa.Column("operator_verdict", sa.String(32), nullable=True))
    op.add_column("log_learning_samples", sa.Column("operator_note", sa.Text(), nullable=True))
    op.add_column("log_learning_samples", sa.Column("operator_verdict_by", sa.String(64), nullable=True))
    op.add_column("log_learning_samples", sa.Column("operator_verdict_at", sa.DateTime(), nullable=True))
    op.create_table(
        "log_learning_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("sample_id", sa.String(36), sa.ForeignKey("log_learning_samples.id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("previous_value_json", sa.Text(), nullable=True),
        sa.Column("new_value_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_log_learning_audit_sample_id", "log_learning_audit", ["sample_id"])


def downgrade() -> None:
    op.drop_index("ix_log_learning_audit_sample_id", table_name="log_learning_audit")
    op.drop_table("log_learning_audit")
    op.drop_column("log_learning_samples", "operator_verdict_at")
    op.drop_column("log_learning_samples", "operator_verdict_by")
    op.drop_column("log_learning_samples", "operator_note")
    op.drop_column("log_learning_samples", "operator_verdict")
