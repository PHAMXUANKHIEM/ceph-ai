"""Add consumer-side idempotency state to the Incident outbox."""

from alembic import op
import sqlalchemy as sa

revision = "m20260923incidentconsumer"
down_revision = "m20260923incidentoutbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incident_outbox", sa.Column("consumer_status", sa.String(length=16), nullable=False, server_default=sa.text("'PENDING'")))
    op.add_column("incident_outbox", sa.Column("consumer_claimed_at", sa.DateTime(), nullable=True))
    op.add_column("incident_outbox", sa.Column("consumer_claim_token", sa.String(length=36), nullable=True))
    op.add_column("incident_outbox", sa.Column("consumer_finished_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("incident_outbox", "consumer_finished_at")
    op.drop_column("incident_outbox", "consumer_claim_token")
    op.drop_column("incident_outbox", "consumer_claimed_at")
    op.drop_column("incident_outbox", "consumer_status")
