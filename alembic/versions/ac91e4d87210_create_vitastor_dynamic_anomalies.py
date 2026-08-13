"""create Vitastor dynamic anomaly storage

Revision ID: ac91e4d87210
Revises: fb13de7a902c
"""
from alembic import op
import sqlalchemy as sa

revision = "ac91e4d87210"
down_revision = "fb13de7a902c"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("vitastor_entity_metric_samples", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("cluster_id", sa.String(36), nullable=False), sa.Column("entity_type", sa.String(16), nullable=False), sa.Column("entity_name", sa.String(255), nullable=False), sa.Column("metrics_json", sa.Text(), nullable=False), sa.Column("collected_at", sa.DateTime(), nullable=False))
    op.create_index("ix_vita_entity_metric_lookup", "vitastor_entity_metric_samples", ["cluster_id", "entity_type", "entity_name", "collected_at"])
    op.create_table("vitastor_anomaly_events", sa.Column("id", sa.String(36), primary_key=True), sa.Column("cluster_id", sa.String(36), nullable=False), sa.Column("entity_type", sa.String(16), nullable=False), sa.Column("entity_name", sa.String(255), nullable=False), sa.Column("metric", sa.String(64), nullable=False), sa.Column("status", sa.String(16), nullable=False), sa.Column("severity", sa.String(16), nullable=False), sa.Column("current_value", sa.Float(), nullable=False), sa.Column("baseline_value", sa.Float(), nullable=False), sa.Column("deviation_ratio", sa.Float(), nullable=False), sa.Column("sample_count", sa.Integer(), nullable=False), sa.Column("explanation", sa.Text(), nullable=False), sa.Column("detected_at", sa.DateTime(), nullable=False), sa.Column("last_seen_at", sa.DateTime(), nullable=False), sa.Column("resolved_at", sa.DateTime(), nullable=True))
    op.create_index("ix_vita_anomaly_cluster_status", "vitastor_anomaly_events", ["cluster_id", "status", "detected_at"])

def downgrade() -> None:
    op.drop_index("ix_vita_anomaly_cluster_status", table_name="vitastor_anomaly_events")
    op.drop_table("vitastor_anomaly_events")
    op.drop_index("ix_vita_entity_metric_lookup", table_name="vitastor_entity_metric_samples")
    op.drop_table("vitastor_entity_metric_samples")
