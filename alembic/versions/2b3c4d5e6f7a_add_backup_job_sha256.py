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
    op.drop_column("backup_jobs", "sha256")
