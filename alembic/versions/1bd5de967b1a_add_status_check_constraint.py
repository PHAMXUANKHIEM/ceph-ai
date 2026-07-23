"""add status check constraint

Revision ID: 1bd5de967b1a
Revises: ab83fa34c932
Create Date: 2026-07-16 11:39:04.382479

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1bd5de967b1a'
down_revision: Union[str, Sequence[str], None] = 'ab83fa34c932'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STATUS_VALUES = (
    "NEW", "DIAGNOSING", "AUTO_FIXED", "PENDING_APPROVAL",
    "APPROVED", "EXECUTING", "RESOLVED", "REJECTED", "FAILED",
)
CONSTRAINT_NAME = "ck_incidents_status_valid"


def upgrade() -> None:
    # Alembic autogenerate does not detect CheckConstraint changes, so this
    # migration is written by hand. SQLite requires batch mode to add
    # constraints to an existing table.
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.create_check_constraint(
            CONSTRAINT_NAME,
            "status IN ('" + "','".join(STATUS_VALUES) + "')",
        )


def downgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_constraint(CONSTRAINT_NAME, type_="check")
