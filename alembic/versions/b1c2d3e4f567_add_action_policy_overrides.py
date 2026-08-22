"""add action policy overrides

Revision ID: b1c2d3e4f567
Revises: a0d1e2f3b456
"""
from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f567"
down_revision = "a0d1e2f3b456"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "action_policy_overrides",
        sa.Column("action_id", sa.String(64), primary_key=True),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "action_policy_override_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("previous_classification", sa.String(16), nullable=False),
        sa.Column("new_classification", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("action_policy_override_audit")
    op.drop_table("action_policy_overrides")
