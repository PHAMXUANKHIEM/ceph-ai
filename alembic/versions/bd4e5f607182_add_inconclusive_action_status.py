"""add fail-closed INCONCLUSIVE autonomous action state

Revision ID: bd4e5f607182
Revises: ac3d4e5f6071
"""
from alembic import op

revision = "bd4e5f607182"
down_revision = "ac3d4e5f6071"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("actions") as batch:
        batch.drop_constraint("ck_actions_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_actions_status_valid",
            "status IN ('PENDING','AUTO_EXECUTED','PENDING_APPROVAL','APPROVED','EXECUTING','INCONCLUSIVE','REJECTED','EXECUTED','FAILED')",
        )


def downgrade():
    with op.batch_alter_table("actions") as batch:
        batch.drop_constraint("ck_actions_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_actions_status_valid",
            "status IN ('PENDING','AUTO_EXECUTED','PENDING_APPROVAL','APPROVED','EXECUTING','REJECTED','EXECUTED','FAILED')",
        )
