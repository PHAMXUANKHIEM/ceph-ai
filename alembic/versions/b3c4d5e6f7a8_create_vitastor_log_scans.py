"""create isolated Vitastor log intelligence scan results

Revision ID: b3c4d5e6f7a8
Revises: f6b9c3d7e102
"""

from alembic import op
import sqlalchemy as sa


revision = "b3c4d5e6f7a8"
down_revision = "f6b9c3d7e102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_log_scans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("window_minutes", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("status", sa.String(16), nullable=False, server_default="OK"),
        sa.Column("lines_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vitastor_log_scans_cluster_id", "vitastor_log_scans", ["cluster_id"])
    op.create_index("ix_vitastor_log_scans_created_at", "vitastor_log_scans", ["created_at"])
    op.create_index("ix_vitastor_log_scan_cluster_created", "vitastor_log_scans", ["cluster_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_vitastor_log_scan_cluster_created", table_name="vitastor_log_scans")
    op.drop_index("ix_vitastor_log_scans_created_at", table_name="vitastor_log_scans")
    op.drop_index("ix_vitastor_log_scans_cluster_id", table_name="vitastor_log_scans")
    op.drop_table("vitastor_log_scans")
