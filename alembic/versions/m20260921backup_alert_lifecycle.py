"""Persist backup alert lifecycle and deduplication state."""

from alembic import op
import sqlalchemy as sa


revision = "m20260921backupalert"
down_revision = "m20260921backupconsistency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_alert_states",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("cluster_id", sa.String(length=36), sa.ForeignKey("clusters.id"), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("resource", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="warning"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OPEN"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("backup_job_id", sa.String(length=36), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=128), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','RESOLVED')", name="ck_backup_alert_states_status_valid"),
    )
    op.create_index(
        "uq_backup_alert_states_scope_key", "backup_alert_states",
        [sa.text("COALESCE(cluster_id, '')"), "dedupe_key"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_backup_alert_states_scope_key", table_name="backup_alert_states")
    op.drop_table("backup_alert_states")
