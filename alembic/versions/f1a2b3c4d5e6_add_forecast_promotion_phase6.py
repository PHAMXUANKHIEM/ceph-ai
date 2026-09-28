"""add paired forecast evaluation and guarded promotion tables

Revision ID: f1a2b3c4d5e6
Revises: f0a1b2c3d4e5
"""

from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_evaluation_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("champion_version", sa.String(128), nullable=False),
        sa.Column("challenger_version", sa.String(128), nullable=False),
        sa.Column("input_window_start", sa.DateTime()),
        sa.Column("input_window_end", sa.DateTime()),
        sa.Column("target_start", sa.DateTime()),
        sa.Column("target_end", sa.DateTime()),
        sa.Column("paired_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_status", sa.String(32), nullable=False),
        sa.Column("quality_ratio", sa.Float(), nullable=False, server_default="0"),
        sa.Column("metrics_json", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("evidence_hash", name="uq_forecast_evaluation_evidence_hash"),
    )
    op.create_index(
        "ix_forecast_evidence_scope", "forecast_evaluation_evidence",
        ["cluster_name", "host", "metric", "horizon_hours"],
    )
    op.create_index(
        "ix_forecast_evidence_expiry", "forecast_evaluation_evidence", ["expires_at"],
    )

    op.create_table(
        "forecast_model_registry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("algorithm", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="SHADOW"),
        sa.Column("model_state_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("previous_version", sa.String(128)),
        sa.Column("activated_at", sa.DateTime()),
        sa.Column("last_health_status", sa.String(32)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "cluster_name", "host", "metric", "horizon_hours", "version",
            name="uq_forecast_model_scope_version",
        ),
    )
    op.create_index(
        "ix_forecast_model_active_scope", "forecast_model_registry",
        ["cluster_name", "host", "metric", "horizon_hours", "status"],
    )
    op.create_index(
        "uq_forecast_model_one_active", "forecast_model_registry",
        ["cluster_name", "host", "metric", "horizon_hours"],
        unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"),
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )

    op.create_table(
        "forecast_promotion_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("evidence_id", sa.String(36), nullable=False),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("candidate_version", sa.String(128), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="APPROVED"),
    )
    op.create_index(
        "ix_forecast_approval_scope_status", "forecast_promotion_approvals",
        ["cluster_name", "host", "metric", "status"],
    )
    op.create_index(
        "ix_forecast_approval_expiry", "forecast_promotion_approvals", ["expires_at"],
    )

    op.create_table(
        "forecast_promotion_audits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("metric", sa.String(64), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("candidate_version", sa.String(128), nullable=False),
        sa.Column("from_version", sa.String(128)),
        sa.Column("to_version", sa.String(128)),
        sa.Column("evidence_hash", sa.String(64)),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_forecast_promotion_audit_scope", "forecast_promotion_audits",
        ["cluster_name", "host", "metric", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_forecast_promotion_audit_scope", table_name="forecast_promotion_audits")
    op.drop_table("forecast_promotion_audits")
    op.drop_index("ix_forecast_approval_expiry", table_name="forecast_promotion_approvals")
    op.drop_index("ix_forecast_approval_scope_status", table_name="forecast_promotion_approvals")
    op.drop_table("forecast_promotion_approvals")
    op.drop_index("uq_forecast_model_one_active", table_name="forecast_model_registry")
    op.drop_index("ix_forecast_model_active_scope", table_name="forecast_model_registry")
    op.drop_table("forecast_model_registry")
    op.drop_index("ix_forecast_evidence_expiry", table_name="forecast_evaluation_evidence")
    op.drop_index("ix_forecast_evidence_scope", table_name="forecast_evaluation_evidence")
    op.drop_table("forecast_evaluation_evidence")
