"""persist LogFinding recovery verification status

Revision ID: 0a1b2c3d4e56
Revises: f0a1b2c3d456
"""
import sqlalchemy as sa
from alembic import op

revision = "0a1b2c3d4e56"
down_revision = "f0a1b2c3d456"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("log_findings", sa.Column("recovery_check_code", sa.String(64)))
    op.add_column("log_findings", sa.Column("recovery_check_summary", sa.Text()))
    op.add_column("log_findings", sa.Column("recovery_checked_at", sa.DateTime()))
    op.add_column("log_findings", sa.Column("recovery_notified_at", sa.DateTime()))


def downgrade() -> None:
    op.drop_column("log_findings", "recovery_notified_at")
    op.drop_column("log_findings", "recovery_checked_at")
    op.drop_column("log_findings", "recovery_check_summary")
    op.drop_column("log_findings", "recovery_check_code")
