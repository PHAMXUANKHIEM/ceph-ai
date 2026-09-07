"""create Vitastor EC safety scans

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
"""

from alembic import op
import sqlalchemy as sa


revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_ec_safety_scans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="INSUFFICIENT_EVIDENCE"),
        sa.Column("pools_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("pools_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("errors_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vitastor_ec_safety_cluster_created", "vitastor_ec_safety_scans", ["cluster_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_vitastor_ec_safety_cluster_created", table_name="vitastor_ec_safety_scans")
    op.drop_table("vitastor_ec_safety_scans")
