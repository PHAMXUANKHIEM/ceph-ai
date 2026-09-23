"""Create the durable RabbitMQ Incident outbox."""

from alembic import op
import sqlalchemy as sa

revision = "m20260923incidentoutbox"
down_revision = "m20260922authsessionversion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("incident_id", sa.String(length=36), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('PENDING','PROCESSING','SENT','DEAD')", name="ck_incident_outbox_status_valid"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_incident_outbox_event_id"),
    )
    op.create_index("ix_incident_outbox_due", "incident_outbox", ["status", "next_attempt_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_incident_outbox_due", table_name="incident_outbox")
    op.drop_table("incident_outbox")
