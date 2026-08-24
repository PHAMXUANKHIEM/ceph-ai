"""add RGW transaction id to access audit

Revision ID: 1b2c3d4e5f67
Revises: 0a1b2c3d4e56
"""
import sqlalchemy as sa
from alembic import op

revision = "1b2c3d4e5f67"
down_revision = "0a1b2c3d4e56"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("rgw_access_audit_events", sa.Column("transaction_id", sa.String(255), nullable=True))
    op.create_index("ix_rgw_access_audit_events_transaction_id", "rgw_access_audit_events", ["transaction_id"])

def downgrade() -> None:
    op.drop_index("ix_rgw_access_audit_events_transaction_id", table_name="rgw_access_audit_events")
    op.drop_column("rgw_access_audit_events", "transaction_id")
