"""add per cluster lab gate

Revision ID: f9c0d1e2a345
Revises: e8b9c0d1f234
"""
import sqlalchemy as sa
from alembic import op

revision = "f9c0d1e2a345"
down_revision = "e8b9c0d1f234"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clusters", sa.Column("autonomy_environment", sa.String(16), nullable=False, server_default="production"))
    op.add_column("clusters", sa.Column("autopilot_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "autopilot_cluster_config_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("previous_environment", sa.String(16), nullable=False),
        sa.Column("new_environment", sa.String(16), nullable=False),
        sa.Column("previous_enabled", sa.Boolean(), nullable=False),
        sa.Column("new_enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("autopilot_cluster_config_audit")
    op.drop_column("clusters", "autopilot_enabled")
    op.drop_column("clusters", "autonomy_environment")
