"""add auditable RBD early forecasts

Revision ID: 5f6a7b8c9d02
Revises: 4e5f6a7b8c91
"""

from alembic import op
import sqlalchemy as sa

revision = "5f6a7b8c9d02"
down_revision = "4e5f6a7b8c91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "volume_early_forecasts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("pool", sa.String(64), nullable=False),
        sa.Column("image", sa.String(128), nullable=False),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("target_at", sa.DateTime(), nullable=False),
        sa.Column("source_latest_at", sa.DateTime(), nullable=False),
        sa.Column("current_value", sa.Float(), nullable=False),
        sa.Column("predicted_value", sa.Float(), nullable=False),
        sa.Column("threshold_type", sa.String(32), nullable=True),
        sa.Column("threshold_value", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("training_samples", sa.Integer(), nullable=False),
        sa.Column("training_window_hours", sa.Integer(), nullable=False),
        sa.Column("seasonal_scope", sa.String(32), nullable=False),
        sa.Column("model_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_volume_early_forecast_idempotency"),
    )
    op.create_index("ix_volume_early_forecasts_generated_at", "volume_early_forecasts", ["generated_at"])
    op.create_index("ix_volume_early_forecasts_status", "volume_early_forecasts", ["status"])
    op.create_index("ix_volume_early_forecast_scope", "volume_early_forecasts", ["cluster_id", "pool", "image", "generated_at"])
    op.create_index("ix_volume_early_forecast_status", "volume_early_forecasts", ["status", "target_at"])


def downgrade() -> None:
    op.drop_table("volume_early_forecasts")
