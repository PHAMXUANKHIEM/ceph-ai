"""create unified Vitastor incident timelines

Revision ID: f8a9b0c1d2e3
Revises: d6e7f8a9b0c1
"""

from alembic import op
import sqlalchemy as sa


revision = "f8a9b0c1d2e3"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_incidents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("fingerprint", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="WARNING"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False, server_default="cluster"),
        sa.Column("entity_name", sa.String(255), nullable=False, server_default="cluster"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("timeline_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("postmortem_text", sa.Text(), nullable=True),
        sa.Column("postmortem_result_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("cluster_id", "fingerprint", name="uq_vitastor_incident_fingerprint"),
    )
    op.create_index("ix_vitastor_incident_cluster_status", "vitastor_incidents", ["cluster_id", "status", "last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_vitastor_incident_cluster_status", table_name="vitastor_incidents")
    op.drop_table("vitastor_incidents")
