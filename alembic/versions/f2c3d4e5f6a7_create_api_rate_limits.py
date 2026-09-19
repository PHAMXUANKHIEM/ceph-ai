"""store shared Dashboard API request throttling

Revision ID: f2c3d4e5f6a7
Revises: f1b2c3d4e5f6
"""

from alembic import op
import sqlalchemy as sa


revision = "f2c3d4e5f6a7"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_rate_limits",
        sa.Column("client_key", sa.String(length=255), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("client_key"),
    )
    op.create_index(
        "ix_api_rate_limits_updated_at",
        "api_rate_limits",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_api_rate_limits_updated_at", table_name="api_rate_limits")
    op.drop_table("api_rate_limits")
