"""add Vitastor remediation post-check state

Revision ID: f4a8b2c6d901
Revises: e1b7f4a92c33
"""

from alembic import op
import sqlalchemy as sa


revision = "f4a8b2c6d901"
down_revision = "e1b7f4a92c33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("vitastor_remediation_actions", recreate="always") as batch:
        batch.drop_constraint("ck_vita_remediation_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_vita_remediation_status_valid",
            "status IN ('PENDING_APPROVAL','AUTO_EXECUTED','APPROVED','REJECTED','EXECUTING','VERIFYING','EXECUTED','FAILED','OBSOLETE')",
        )
    op.add_column(
        "vitastor_remediation_actions",
        sa.Column("verification_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("vitastor_remediation_actions", sa.Column("verification_error", sa.Text(), nullable=True))
    op.add_column("vitastor_remediation_actions", sa.Column("verified_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("vitastor_remediation_actions", "verified_at")
    op.drop_column("vitastor_remediation_actions", "verification_error")
    op.drop_column("vitastor_remediation_actions", "verification_attempts")
