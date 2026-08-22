"""add append-only autopilot kill-switch audit

Revision ID: ac3d4e5f6071
Revises: 9b2c3d4e5f60
"""
from alembic import op
import sqlalchemy as sa

revision = "ac3d4e5f6071"
down_revision = "9b2c3d4e5f60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "autopilot_config_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("previous_enabled", sa.Boolean(), nullable=False),
        sa.Column("new_enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("autopilot_config_audit")
