"""store dashboard login throttling in the shared database

Revision ID: f1b2c3d4e5f6
Revises: f0a1b2c3d4f7
"""

from alembic import op
import sqlalchemy as sa


revision = "f1b2c3d4e5f6"
down_revision = "f0a1b2c3d4f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_login_rate_limits",
        sa.Column("client_key", sa.String(length=255), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("client_key"),
    )
    op.create_index(
        "ix_auth_login_rate_limits_locked_until",
        "auth_login_rate_limits",
        ["locked_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_auth_login_rate_limits_locked_until",
        table_name="auth_login_rate_limits",
    )
    op.drop_table("auth_login_rate_limits")
