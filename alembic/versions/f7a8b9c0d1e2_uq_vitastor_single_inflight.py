"""make Vitastor lifecycle operation admission atomic

Revision ID: f7a8b9c0d1e2
Revises: 6c8d9e0f1234
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "6c8d9e0f1234"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_vitastor_single_inflight",
        "vitastor_operations",
        [sa.literal_column("(1)")],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING_APPROVAL','RUNNING')"),
        postgresql_where=sa.text("status IN ('PENDING_APPROVAL','RUNNING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_vitastor_single_inflight", table_name="vitastor_operations")
