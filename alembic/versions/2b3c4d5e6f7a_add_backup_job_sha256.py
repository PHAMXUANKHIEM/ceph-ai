"""store source checksum on backup jobs

Revision ID: 2b3c4d5e6f7a
Revises: 1a2b3c4d5e6f
"""

import sqlalchemy as sa
from alembic import op


revision = "2b3c4d5e6f7a"
down_revision = "1a2b3c4d5e6f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backup_jobs", sa.Column("sha256", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_index("uq_backup_jobs_active_rbd_run", table_name="backup_jobs")
    with op.batch_alter_table("backup_jobs") as batch_op:
        batch_op.drop_column("sha256")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_backup_jobs_active_rbd_run
        ON backup_jobs (COALESCE(cluster_id, ''), pool, image)
        WHERE status = 'RUNNING'
        """
    )
