"""create Vitastor node hardware metrics

Revision ID: f821bd0a734e
Revises: e731fa2c8160
"""
import sqlalchemy as sa
from alembic import op
revision = "f821bd0a734e"
down_revision = "e731fa2c8160"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("vitastor_node_metric_samples", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("cluster_id", sa.String(36), nullable=False), sa.Column("host", sa.String(255), nullable=False), sa.Column("osd_processes", sa.Integer(), nullable=False), sa.Column("cpu_percent", sa.Float(), nullable=False), sa.Column("ram_bytes", sa.BigInteger(), nullable=False), sa.Column("max_temperature_c", sa.Float(), nullable=True), sa.Column("max_wear_percent", sa.Float(), nullable=True), sa.Column("media_errors", sa.BigInteger(), nullable=False), sa.Column("smart_failing", sa.Boolean(), nullable=False), sa.Column("raw_json", sa.Text(), nullable=False), sa.Column("collected_at", sa.DateTime(), nullable=False))
    op.create_index("ix_vitastor_node_metric_cluster_host_time", "vitastor_node_metric_samples", ["cluster_id", "host", "collected_at"])

def downgrade() -> None:
    op.drop_index("ix_vitastor_node_metric_cluster_host_time", table_name="vitastor_node_metric_samples")
    op.drop_table("vitastor_node_metric_samples")
