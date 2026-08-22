"""add append-only incident timeline events

Revision ID: 7f8091a2b3c4
Revises: 6e7f8091a2b3
"""
from alembic import op
import sqlalchemy as sa

revision = "7f8091a2b3c4"
down_revision = "6e7f8091a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_timeline_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=True),
        sa.Column("source_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source_type", "source_id", name="uq_incident_timeline_event_source"),
    )
    op.create_index("ix_incident_timeline_event_order", "incident_timeline_events", ["incident_id", "created_at"])


def downgrade() -> None:
    op.drop_table("incident_timeline_events")
