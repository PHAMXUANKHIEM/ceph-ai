"""create immediate RGW analysis jobs

Revision ID: f0a1b2c3d456
Revises: e9f0a1b2c345
"""
import sqlalchemy as sa
from alembic import op

revision = "f0a1b2c3d456"
down_revision = "e9f0a1b2c345"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rgw_analysis_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("source_event_id", sa.String(36), sa.ForeignKey("rgw_error_notifications.id"), nullable=False),
        sa.Column("signature", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="QUEUED"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ingest_run_id", sa.String(36), sa.ForeignKey("log_ingest_runs.id")),
        sa.Column("finding_id", sa.String(36), sa.ForeignKey("log_findings.id")),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
    )
    op.create_index("ix_rgw_analysis_job_pending", "rgw_analysis_jobs", ["status", "created_at"])
    op.create_index("ix_rgw_analysis_job_signature", "rgw_analysis_jobs", ["signature", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_rgw_analysis_job_signature", table_name="rgw_analysis_jobs")
    op.drop_index("ix_rgw_analysis_job_pending", table_name="rgw_analysis_jobs")
    op.drop_table("rgw_analysis_jobs")
