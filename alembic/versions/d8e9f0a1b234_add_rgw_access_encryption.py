"""add RGW access encryption classification

Revision ID: d8e9f0a1b234
Revises: c7d8e9f0a123
"""
import sqlalchemy as sa
from alembic import op

revision = "d8e9f0a1b234"
down_revision = "c7d8e9f0a123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rgw_access_audit_events", sa.Column("encryption", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("rgw_access_audit_events", "encryption")
