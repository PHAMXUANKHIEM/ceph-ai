"""Add incident_evidence for read-only pre-diagnosis evidence (autonomy WP3.3)."""

from alembic import op
import sqlalchemy as sa


revision = "m20260928incidentevidence"
down_revision = "m20260925clusteraccess"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "incident_evidence",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("incident_id", sa.String(36), nullable=False),
        sa.Column("runbook", sa.String(64), nullable=False),
        sa.Column("collector_id", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("output_redacted", sa.Text(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incident_evidence_incident", "incident_evidence", ["incident_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_incident_evidence_incident", table_name="incident_evidence")
    op.drop_table("incident_evidence")
