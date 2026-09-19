"""create append-only forecast promotion history

Revision ID: a9b8c7d6e5f4
Revises: d1e2f3a456b7
"""

from alembic import op
import sqlalchemy as sa


revision = "a9b8c7d6e5f4"
down_revision = "d1e2f3a456b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_model_evaluations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_model_id", sa.String(length=36), nullable=False),
        sa.Column("active_model_id", sa.String(length=36), nullable=False),
        sa.Column("target_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("active_evaluated", sa.Integer(), nullable=False),
        sa.Column("candidate_evaluated", sa.Integer(), nullable=False),
        sa.Column("active_mae", sa.Float(), nullable=True),
        sa.Column("candidate_mae", sa.Float(), nullable=True),
        sa.Column("active_rmse", sa.Float(), nullable=True),
        sa.Column("candidate_rmse", sa.Float(), nullable=True),
        sa.Column("active_smape", sa.Float(), nullable=True),
        sa.Column("candidate_smape", sa.Float(), nullable=True),
        sa.Column("active_bias", sa.Float(), nullable=True),
        sa.Column("candidate_bias", sa.Float(), nullable=True),
        sa.Column("active_false_positive_rate", sa.Float(), nullable=True),
        sa.Column("candidate_false_positive_rate", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["candidate_model_id"], ["forecast_model_registry.id"]),
        sa.ForeignKeyConstraint(["active_model_id"], ["forecast_model_registry.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "candidate_model_id", "active_model_id", "target_at",
            name="uq_forecast_model_evaluation_target",
        ),
    )
    op.create_index(
        "ix_forecast_model_evaluation_candidate_time",
        "forecast_model_evaluations", ["candidate_model_id", "target_at"],
    )
    op.create_table(
        "forecast_model_promotion_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("candidate_model_id", sa.String(length=36), nullable=False),
        sa.Column("previous_active_model_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_model_id"], ["forecast_model_registry.id"]),
        sa.ForeignKeyConstraint(["previous_active_model_id"], ["forecast_model_registry.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_forecast_model_promotion_audit_candidate_time",
        "forecast_model_promotion_audits", ["candidate_model_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_forecast_model_promotion_audit_candidate_time",
        table_name="forecast_model_promotion_audits",
    )
    op.drop_table("forecast_model_promotion_audits")
    op.drop_index(
        "ix_forecast_model_evaluation_candidate_time",
        table_name="forecast_model_evaluations",
    )
    op.drop_table("forecast_model_evaluations")
