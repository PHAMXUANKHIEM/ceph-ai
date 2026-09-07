"""create Vitastor config drift scans

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
"""

from alembic import op
import sqlalchemy as sa


revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_config_drift_scans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="OK"),
        sa.Column("hosts_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("facts_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("errors_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vitastor_config_drift_cluster_created", "vitastor_config_drift_scans", ["cluster_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_vitastor_config_drift_cluster_created", table_name="vitastor_config_drift_scans")
    op.drop_table("vitastor_config_drift_scans")
