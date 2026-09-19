"""widen backup job byte counts

Revision ID: 3c4d5e6f7a8b
Revises: 2b3c4d5e6f7a
"""

import sqlalchemy as sa
from alembic import op


revision = "3c4d5e6f7a8b"
down_revision = "2b3c4d5e6f7a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_backup_jobs_active_rbd_run", table_name="backup_jobs")
    with op.batch_alter_table("backup_jobs") as batch_op:
        batch_op.alter_column(
            "size_bytes",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_backup_jobs_active_rbd_run
        ON backup_jobs (COALESCE(cluster_id, ''), pool, image)
        WHERE status = 'RUNNING'
        """
    )


def downgrade() -> None:
    op.drop_index("uq_backup_jobs_active_rbd_run", table_name="backup_jobs")
    with op.batch_alter_table("backup_jobs") as batch_op:
        batch_op.alter_column(
            "size_bytes",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_backup_jobs_active_rbd_run
        ON backup_jobs (COALESCE(cluster_id, ''), pool, image)
        WHERE status = 'RUNNING'
        """
    )
