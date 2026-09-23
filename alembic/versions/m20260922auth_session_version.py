"""Add per-account session versions for immediate revocation."""

from alembic import op
import sqlalchemy as sa


revision = "m20260922authsessionversion"
down_revision = "m20260921forecasthorizons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "vitastor_users",
        sa.Column("session_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("vitastor_users", "session_version")
    op.drop_column("users", "session_version")
