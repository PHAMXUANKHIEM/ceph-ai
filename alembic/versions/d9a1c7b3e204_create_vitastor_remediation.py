"""create Vitastor remediation actions + audit trail

Revision ID: d9a1c7b3e204
Revises: c7d4e5f6a701
"""
from alembic import op
import sqlalchemy as sa

revision = "d9a1c7b3e204"
down_revision = "c7d4e5f6a701"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_remediation_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="MONITOR"),
        sa.Column("source_id", sa.String(36), nullable=True),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING_APPROVAL"),
        sa.Column("target_host", sa.String(255), nullable=False, server_default=""),
        sa.Column("action_params", sa.Text(), nullable=True),
        sa.Column("proposed_command", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("result_output", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.String(255), nullable=False, server_default=""),
        sa.Column("requested_by", sa.String(64), nullable=False, server_default="vitastor-monitor"),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("telegram_message_ids", sa.Text(), nullable=True),
        sa.Column("telegram_notified_at", sa.DateTime(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("classification IN ('SAFE','RISKY')", name="ck_vita_remediation_classification_valid"),
        sa.CheckConstraint(
            "status IN ('PENDING_APPROVAL','AUTO_EXECUTED','APPROVED','REJECTED','EXECUTING','EXECUTED','FAILED')",
            name="ck_vita_remediation_status_valid",
        ),
    )
    op.create_index("ix_vita_remediation_cluster_status", "vitastor_remediation_actions", ["cluster_id", "status", "created_at"])
    op.create_table(
        "vitastor_audit_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("action_pk", sa.String(36), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vita_audit_cluster_created", "vitastor_audit_entries", ["cluster_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_vita_audit_cluster_created", table_name="vitastor_audit_entries")
    op.drop_table("vitastor_audit_entries")
    op.drop_index("ix_vita_remediation_cluster_status", table_name="vitastor_remediation_actions")
    op.drop_table("vitastor_remediation_actions")
