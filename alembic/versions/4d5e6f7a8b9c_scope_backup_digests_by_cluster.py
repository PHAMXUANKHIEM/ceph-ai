"""scope backup digests by cluster

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
"""

import sqlalchemy as sa
from alembic import op


revision = "4d5e6f7a8b9c"
down_revision = "3c4d5e6f7a8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backup_digest_logs", sa.Column("cluster_id", sa.String(length=36), nullable=True))
    with op.batch_alter_table("backup_digest_logs") as batch_op:
        batch_op.create_foreign_key(
            "fk_backup_digest_logs_cluster_id", "clusters", ["cluster_id"], ["id"]
        )
    op.create_index(
        "ix_backup_digest_logs_cluster_created_at",
        "backup_digest_logs",
        ["cluster_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_backup_digest_logs_cluster_created_at", table_name="backup_digest_logs")
    with op.batch_alter_table("backup_digest_logs") as batch_op:
        batch_op.drop_constraint("fk_backup_digest_logs_cluster_id", type_="foreignkey")
    op.drop_column("backup_digest_logs", "cluster_id")
