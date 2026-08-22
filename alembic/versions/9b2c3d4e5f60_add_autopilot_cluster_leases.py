"""add autonomous cluster execution leases and EXECUTING action state

Revision ID: 9b2c3d4e5f60
Revises: 8a1b2c3d4e5f
"""
from alembic import op
import sqlalchemy as sa

revision = "9b2c3d4e5f60"
down_revision = "8a1b2c3d4e5f"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("actions") as batch:
        batch.drop_constraint("ck_actions_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_actions_status_valid",
            "status IN ('PENDING','AUTO_EXECUTED','PENDING_APPROVAL','APPROVED','EXECUTING','REJECTED','EXECUTED','FAILED')",
        )
    op.create_table(
        "autopilot_leases",
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), primary_key=True),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id"), nullable=False, unique=True),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("autopilot_leases")
    with op.batch_alter_table("actions") as batch:
        batch.drop_constraint("ck_actions_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_actions_status_valid",
            "status IN ('PENDING','AUTO_EXECUTED','PENDING_APPROVAL','APPROVED','REJECTED','EXECUTED','FAILED')",
        )
