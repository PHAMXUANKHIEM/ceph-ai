"""add per-volume baseline learning

Revision ID: 4e5f6a7b8c91
Revises: 3d4e5f6a7b80
"""
from alembic import op
import sqlalchemy as sa

revision = "4e5f6a7b8c91"
down_revision = "3d4e5f6a7b80"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "volume_forecast_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("pool", sa.String(64), nullable=False),
        sa.Column("image", sa.String(128), nullable=False),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=False),
        sa.Column("predicted_at", sa.DateTime(), nullable=False),
        sa.Column("target_at", sa.DateTime(), nullable=False),
        sa.Column("current_value", sa.Float(), nullable=False),
        sa.Column("predicted_value", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("seasonal_scope", sa.String(32), nullable=False),
        sa.Column("training_samples", sa.Integer(), nullable=False),
        sa.Column("actual_value", sa.Float(), nullable=True),
        sa.Column("absolute_error", sa.Float(), nullable=True),
        sa.Column("percentage_error", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_volume_forecast_idempotency"),
    )
    op.create_index("ix_volume_forecast_runs_predicted_at", "volume_forecast_runs", ["predicted_at"])
    op.create_index("ix_volume_forecast_runs_target_at", "volume_forecast_runs", ["target_at"])
    op.create_index("ix_volume_forecast_runs_status", "volume_forecast_runs", ["status"])
    op.create_index("ix_volume_forecast_due", "volume_forecast_runs", ["status", "target_at"])
    op.create_index("ix_volume_forecast_scope", "volume_forecast_runs", ["cluster_id", "pool", "image", "metric"])

    op.create_table(
        "volume_model_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("pool", sa.String(64), nullable=False),
        sa.Column("image", sa.String(128), nullable=False),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("algorithm", sa.String(32), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=False),
        sa.Column("evaluated_count", sa.Integer(), nullable=False),
        sa.Column("mean_absolute_error", sa.Float(), nullable=True),
        sa.Column("mean_percentage_error", sa.Float(), nullable=True),
        sa.Column("last_absolute_error", sa.Float(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("cluster_id", "pool", "image", "metric", "algorithm", "window_hours", name="uq_volume_model_identity"),
    )
    op.create_index("ix_volume_model_scope", "volume_model_states", ["cluster_id", "pool", "image", "metric"])


def downgrade() -> None:
    op.drop_table("volume_model_states")
    op.drop_table("volume_forecast_runs")
