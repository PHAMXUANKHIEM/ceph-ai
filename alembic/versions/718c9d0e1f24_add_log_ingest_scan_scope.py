"""add log ingest scan scope

Revision ID: 718c9d0e1f24
Revises: 607b8c9d0e13
"""
from alembic import op
import sqlalchemy as sa

revision = "718c9d0e1f24"
down_revision = "607b8c9d0e13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "log_ingest_runs",
        sa.Column("scan_scope", sa.String(length=16), nullable=False, server_default="FULL"),
    )
    # Historical rows did not record whether they were targeted. Treat them
    # conservatively: the first post-upgrade full scan uses the fallback
    # window and then establishes a trustworthy FULL checkpoint.
    op.execute("UPDATE log_ingest_runs SET scan_scope = 'LEGACY'")


def downgrade() -> None:
    op.drop_column("log_ingest_runs", "scan_scope")
