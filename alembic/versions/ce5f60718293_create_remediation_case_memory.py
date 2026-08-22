"""create remediation case memory and playbook stats

Revision ID: ce5f60718293
Revises: bd4e5f607182
"""
import sqlalchemy as sa
from alembic import op

revision = "ce5f60718293"
down_revision = "bd4e5f607182"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "remediation_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id"), nullable=False, unique=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id")),
        sa.Column("fault_family", sa.String(64), nullable=False),
        sa.Column("entity_keys_json", sa.Text()), sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("ceph_version", sa.String(64)), sa.Column("deployment_mode", sa.String(32)),
        sa.Column("topology_snapshot_json", sa.Text()), sa.Column("diagnosis", sa.Text()),
        sa.Column("diagnosis_confidence", sa.Float()), sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("model_provider", sa.String(32)), sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("autonomy_decision", sa.String(32), nullable=False),
        sa.Column("playbook_version", sa.String(32), nullable=False),
        sa.Column("preflight_snapshot_json", sa.Text()), sa.Column("command_preview_hash", sa.String(64)),
        sa.Column("pre_state_json", sa.Text()), sa.Column("post_state_json", sa.Text()),
        sa.Column("rollback_state_json", sa.Text()), sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("side_effects_json", sa.Text()), sa.Column("started_at", sa.DateTime()),
        sa.Column("executed_at", sa.DateTime()), sa.Column("verified_at", sa.DateTime()),
        sa.Column("recovery_seconds", sa.Integer()), sa.Column("regressed_1h", sa.Boolean()),
        sa.Column("regressed_24h", sa.Boolean()), sa.Column("regressed_7d", sa.Boolean()),
        sa.Column("operator_verdict", sa.String(32)), sa.Column("operator_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_remediation_cases_evidence_fingerprint", "remediation_cases", ["evidence_fingerprint"])
    op.create_table(
        "playbook_stats",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("playbook_id", sa.String(64), nullable=False),
        sa.Column("playbook_version", sa.String(32), nullable=False), sa.Column("scope_key", sa.String(256), nullable=False),
        sa.Column("proposed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("executed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verified_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inconclusive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trust_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("maturity_level", sa.String(16), nullable=False, server_default="L0"),
        sa.Column("last_failure_at", sa.DateTime()), sa.Column("auto_disabled_reason", sa.Text()),
        sa.Column("promotion_candidate_at", sa.DateTime()), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("playbook_id", "playbook_version", "scope_key", name="uq_playbook_stats_scope"),
    )


def downgrade():
    op.drop_table("playbook_stats")
    op.drop_index("ix_remediation_cases_evidence_fingerprint", table_name="remediation_cases")
    op.drop_table("remediation_cases")
