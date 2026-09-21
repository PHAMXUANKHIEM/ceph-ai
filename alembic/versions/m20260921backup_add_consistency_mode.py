"""Persist whether a backup was crash- or application-consistent."""

from alembic import op
import sqlalchemy as sa


revision = "m20260921backupconsistency"
down_revision = "m20260921onlinebackend"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backup_jobs", sa.Column("consistency_mode", sa.String(length=32), nullable=True))
    op.execute(
        "UPDATE backup_jobs SET consistency_mode = 'crash-consistent' "
        "WHERE consistency_mode IS NULL"
    )


def downgrade() -> None:
    op.drop_column("backup_jobs", "consistency_mode")
