"""add append-only audit metadata for Dashboard mutations

Revision ID: f3d4e5f6a7b8
Revises: f2c3d4e5f6a7
"""

from alembic import op
import sqlalchemy as sa


revision = "f3d4e5f6a7b8"
down_revision = "f2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "security_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_security_audit_events_actor", "security_audit_events", ["actor"])
    op.create_index("ix_security_audit_events_path", "security_audit_events", ["path"])
    op.create_index("ix_security_audit_events_request_id", "security_audit_events", ["request_id"])
    op.create_index("ix_security_audit_events_created_at", "security_audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_security_audit_events_created_at", table_name="security_audit_events")
    op.drop_index("ix_security_audit_events_request_id", table_name="security_audit_events")
    op.drop_index("ix_security_audit_events_path", table_name="security_audit_events")
    op.drop_index("ix_security_audit_events_actor", table_name="security_audit_events")
    op.drop_table("security_audit_events")
