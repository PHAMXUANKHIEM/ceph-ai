"""create backup metadata artifact manifests

Revision ID: 5e6f7a8b9c0d
Revises: 4d5e6f7a8b9c
"""

import sqlalchemy as sa
from alembic import op


revision = "5e6f7a8b9c0d"
down_revision = "4d5e6f7a8b9c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_metadata_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("backup_job_id", sa.String(length=36), nullable=False),
        sa.Column("artifact_name", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["backup_job_id"], ["backup_jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "backup_job_id",
            "artifact_name",
            name="uq_backup_metadata_artifacts_job_name",
        ),
    )
    op.create_index(
        "ix_backup_metadata_artifacts_backup_job_id",
        "backup_metadata_artifacts",
        ["backup_job_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backup_metadata_artifacts_backup_job_id",
        table_name="backup_metadata_artifacts",
    )
    op.drop_table("backup_metadata_artifacts")
