"""add server-owned semantic identity to log findings

Revision ID: 8d22c1a4f901
Revises: 2c06619773a5
"""

from alembic import op
import sqlalchemy as sa

revision = "8d22c1a4f901"
down_revision = "2c06619773a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("log_findings", sa.Column("fault_family", sa.String(length=64), nullable=True))
    op.add_column("log_findings", sa.Column("semantic_entities_json", sa.Text(), nullable=True))
    op.create_index("ix_log_findings_fault_family", "log_findings", ["fault_family"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_log_findings_fault_family", table_name="log_findings")
    op.drop_column("log_findings", "semantic_entities_json")
    op.drop_column("log_findings", "fault_family")
