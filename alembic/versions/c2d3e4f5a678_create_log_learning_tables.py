"""create supervised daemon-log learning tables

Revision ID: c2d3e4f5a678
Revises: b1c2d3e4f567
"""
from alembic import op
import sqlalchemy as sa

revision = "c2d3e4f5a678"
down_revision = "b1c2d3e4f567"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "log_learning_samples",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("log_finding_id", sa.String(36), sa.ForeignKey("log_findings.id"), nullable=False),
        sa.Column("ingest_run_id", sa.String(36), sa.ForeignKey("log_ingest_runs.id"), nullable=False),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=True),
        sa.Column("remediation_case_id", sa.String(36), sa.ForeignKey("remediation_cases.id"), nullable=True),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("daemon_type", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("daemon_id", sa.String(128), nullable=True),
        sa.Column("host", sa.String(255), nullable=True),
        sa.Column("fault_family", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("entity_key", sa.String(255), nullable=False, server_default="unknown"),
        sa.Column("pattern_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("ingest_status", sa.String(16), nullable=False),
        sa.Column("parser_version", sa.String(32), nullable=False),
        sa.Column("semantic_version", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("diagnosis_confidence", sa.String(16), nullable=True),
        sa.Column("recommended_playbook_id", sa.String(64), nullable=True),
        sa.Column("playbook_version", sa.String(32), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="CANDIDATE"),
        sa.Column("label", sa.String(32), nullable=False, server_default="UNVERIFIED"),
        sa.Column("eligible_for_learning", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("exclusion_reason", sa.Text(), nullable=True),
        sa.Column("outcome_source", sa.String(32), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("regressed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("log_finding_id", name="uq_log_learning_samples_finding"),
    )
    op.create_index("ix_log_learning_samples_scope", "log_learning_samples", ["cluster_id", "daemon_type", "fault_family"])
    op.create_index("ix_log_learning_samples_eligibility", "log_learning_samples", ["eligible_for_learning", "label"])
    op.create_index("ix_log_learning_samples_evidence_fingerprint", "log_learning_samples", ["evidence_fingerprint"])
    op.create_table(
        "log_fault_stats",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("daemon_type", sa.String(32), nullable=False),
        sa.Column("fault_family", sa.String(64), nullable=False),
        sa.Column("playbook_id", sa.String(64), nullable=False, server_default="observation_only"),
        sa.Column("playbook_version", sa.String(32), nullable=False, server_default="none"),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verified_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inconclusive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trust_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("promotion_candidate_at", sa.DateTime(), nullable=True),
        sa.Column("promotion_blocked_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("cluster_id", "daemon_type", "fault_family", "playbook_id", "playbook_version", name="uq_log_fault_stats_scope"),
    )


def downgrade() -> None:
    op.drop_table("log_fault_stats")
    op.drop_index("ix_log_learning_samples_evidence_fingerprint", table_name="log_learning_samples")
    op.drop_index("ix_log_learning_samples_eligibility", table_name="log_learning_samples")
    op.drop_index("ix_log_learning_samples_scope", table_name="log_learning_samples")
    op.drop_table("log_learning_samples")
