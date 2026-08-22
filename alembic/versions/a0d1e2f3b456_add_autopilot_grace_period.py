"""add autopilot grace period

Revision ID: a0d1e2f3b456
Revises: f9c0d1e2a345
"""
import sqlalchemy as sa
from alembic import op

revision = "a0d1e2f3b456"
down_revision = "f9c0d1e2a345"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("actions") as batch:
        batch.drop_constraint("ck_actions_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_actions_status_valid",
            "status IN ('PENDING','AUTO_EXECUTED','PENDING_APPROVAL','APPROVED','EXECUTING','GRACE_PENDING','INCONCLUSIVE','REJECTED','EXECUTED','FAILED')",
        )
        batch.add_column(sa.Column("grace_until", sa.DateTime()))
        batch.add_column(sa.Column("cancelled_at", sa.DateTime()))
        batch.add_column(sa.Column("cancelled_by", sa.String(64)))
    with op.batch_alter_table("incidents") as batch:
        batch.drop_constraint("ck_incidents_status_valid", type_="check")
        batch.create_check_constraint(
            "ck_incidents_status_valid",
            "status IN ('NEW','DIAGNOSING','AUTO_FIXED','PENDING_APPROVAL','APPROVED','EXECUTING','GRACE_PENDING','VERIFYING','RESOLVED','REJECTED','FAILED')",
        )
    op.drop_index("uq_actions_idempotency_key_inflight", table_name="actions")
    op.create_index(
        "uq_actions_idempotency_key_inflight", "actions", ["idempotency_key"], unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED','GRACE_PENDING')"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED','GRACE_PENDING')"),
    )


def downgrade():
    op.drop_index("uq_actions_idempotency_key_inflight", table_name="actions")
    op.create_index(
        "uq_actions_idempotency_key_inflight", "actions", ["idempotency_key"], unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED')"),
        sqlite_where=sa.text("idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED')"),
    )
    with op.batch_alter_table("incidents") as batch:
        batch.drop_constraint("ck_incidents_status_valid", type_="check")
        batch.create_check_constraint("ck_incidents_status_valid", "status IN ('NEW','DIAGNOSING','AUTO_FIXED','PENDING_APPROVAL','APPROVED','EXECUTING','VERIFYING','RESOLVED','REJECTED','FAILED')")
    with op.batch_alter_table("actions") as batch:
        batch.drop_column("cancelled_by")
        batch.drop_column("cancelled_at")
        batch.drop_column("grace_until")
        batch.drop_constraint("ck_actions_status_valid", type_="check")
        batch.create_check_constraint("ck_actions_status_valid", "status IN ('PENDING','AUTO_EXECUTED','PENDING_APPROVAL','APPROVED','EXECUTING','INCONCLUSIVE','REJECTED','EXECUTED','FAILED')")
