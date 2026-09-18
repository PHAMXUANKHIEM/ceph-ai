"""create forecast model registry

Revision ID: c0d1e2f3a456
Revises: b7c8d9e0f1a2
"""

from alembic import op
import sqlalchemy as sa


revision = "c0d1e2f3a456"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecast_model_registry",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("feature_schema", sa.String(length=64), nullable=False),
        sa.Column("training_window_hours", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("promotion_reason", sa.Text(), nullable=True),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("active_since", sa.DateTime(), nullable=True),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('CANDIDATE','SHADOW','ACTIVE','RETIRED','BLOCKED')",
            name="ck_forecast_model_registry_status_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type", "scope_key", "name", "version",
            name="uq_forecast_model_registry_identity",
        ),
    )
    op.create_index(
        "ix_forecast_model_registry_scope_status",
        "forecast_model_registry", ["scope_type", "scope_key", "status"],
    )
    op.create_index(
        "ix_forecast_model_registry_status",
        "forecast_model_registry", ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_forecast_model_registry_status", table_name="forecast_model_registry")
    op.drop_index("ix_forecast_model_registry_scope_status", table_name="forecast_model_registry")
    op.drop_table("forecast_model_registry")
