"""Merge the capacity and remediation runtime migrations."""

from alembic import op


revision = "m20260925merge"
down_revision = ("m20260925capacity", "m20260925remediation")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
