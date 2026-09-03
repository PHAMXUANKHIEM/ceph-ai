"""add race-safe active RBD backup guard

Revision ID: 1a2b3c4d5e6f
Revises: d1e2f3a4b5c6
"""

from alembic import op


revision = "1a2b3c4d5e6f"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_backup_jobs_active_rbd_run
        ON backup_jobs (COALESCE(cluster_id, ''), pool, image)
        WHERE status = 'RUNNING'
        """
    )


def downgrade() -> None:
    op.drop_index("uq_backup_jobs_active_rbd_run", table_name="backup_jobs")
