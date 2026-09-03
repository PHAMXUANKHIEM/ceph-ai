"""keep lifecycle idempotency lock while an action is executing

Revision ID: 5f6a7b8c9d03
Revises: 5e6f7a8b9c0d
"""

import sqlalchemy as sa
from alembic import op


revision = "5f6a7b8c9d03"
down_revision = "5e6f7a8b9c0d"
branch_labels = None
depends_on = None


_IN_FLIGHT = "idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED','EXECUTING','GRACE_PENDING')"
_BEFORE = "idempotency_key IS NOT NULL AND status IN ('PENDING','PENDING_APPROVAL','APPROVED','GRACE_PENDING')"


def upgrade() -> None:
    op.drop_index("uq_actions_idempotency_key_inflight", table_name="actions")
    op.create_index(
        "uq_actions_idempotency_key_inflight",
        "actions",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text(_IN_FLIGHT),
        sqlite_where=sa.text(_IN_FLIGHT),
    )


def downgrade() -> None:
    op.drop_index("uq_actions_idempotency_key_inflight", table_name="actions")
    op.create_index(
        "uq_actions_idempotency_key_inflight",
        "actions",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text(_BEFORE),
        sqlite_where=sa.text(_BEFORE),
    )
