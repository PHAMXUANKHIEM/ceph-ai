"""add action expiry and idempotency key

Revision ID: 5bdecca5014e
Revises: 18f374b79a75
Create Date: 2026-08-18
"""

from alembic import op
import sqlalchemy as sa

revision = "5bdecca5014e"
down_revision = "18f374b79a75"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("actions", sa.Column("expires_at", sa.DateTime(), nullable=True))
    op.add_column("actions", sa.Column("idempotency_key", sa.String(64), nullable=True))
    # Scoped to in-flight statuses only, NOT a permanent global uniqueness
    # guarantee — see shared/models.py::Action's own table_args comment for
    # why a permanent constraint would wrongly block a legitimate future
    # re-proposal of the same command once an earlier one already finished.
    op.create_index(
        "uq_actions_idempotency_key_inflight",
        "actions",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text(
            "idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED')"
        ),
        sqlite_where=sa.text(
            "idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED')"
        ),
    )
    # classification's CheckConstraint now also allows READ_ONLY/DESTRUCTIVE
    # — SQLite requires batch mode to alter constraints on an existing
    # table (see 1bd5de967b1a_add_status_check_constraint.py's own
    # precedent), and Postgres has no ALTER CHECK CONSTRAINT either way, so
    # drop + recreate under the same name via batch_alter_table (works on
    # both dialects).
    with op.batch_alter_table("actions") as batch_op:
        batch_op.drop_constraint("ck_actions_classification_valid", type_="check")
        batch_op.create_check_constraint(
            "ck_actions_classification_valid",
            "classification IN ('READ_ONLY','SAFE','RISKY','DESTRUCTIVE')",
        )


def downgrade() -> None:
    with op.batch_alter_table("actions") as batch_op:
        batch_op.drop_constraint("ck_actions_classification_valid", type_="check")
        batch_op.create_check_constraint(
            "ck_actions_classification_valid",
            "classification IN ('SAFE','RISKY')",
        )
    op.drop_index("uq_actions_idempotency_key_inflight", table_name="actions")
    op.drop_column("actions", "idempotency_key")
    op.drop_column("actions", "expires_at")
