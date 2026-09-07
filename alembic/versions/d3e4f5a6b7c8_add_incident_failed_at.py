"""record the terminal failure timestamp for Incident retry cooldowns

Revision ID: d3e4f5a6b7c8
Revises: d2e3f4a5b6c7
"""

from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("failed_at", sa.DateTime(), nullable=True))
    # Historical rows have no exact transition event.  updated_at is the
    # closest available value and preserves the existing retry semantics.
    op.execute("UPDATE incidents SET failed_at = updated_at WHERE status = 'FAILED'")
    op.create_index("ix_incidents_failed_at", "incidents", ["failed_at"])


def downgrade() -> None:
    op.drop_index("ix_incidents_failed_at", table_name="incidents")
    op.drop_column("incidents", "failed_at")
