"""create Vitastor network metrics

Revision ID: fb13de7a902c
Revises: fa912c8e047b
"""
import sqlalchemy as sa
from alembic import op
revision = "fb13de7a902c"
down_revision = "fa912c8e047b"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table("vitastor_network_metric_samples", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("cluster_id", sa.String(36), nullable=False), sa.Column("source", sa.String(255), nullable=False), sa.Column("target", sa.String(255), nullable=False), sa.Column("reachable", sa.Boolean(), nullable=False), sa.Column("rtt_ms", sa.Float(), nullable=True), sa.Column("jumbo_9000", sa.Boolean(), nullable=False), sa.Column("interface_json", sa.Text(), nullable=False), sa.Column("collected_at", sa.DateTime(), nullable=False))
    op.create_index("ix_vitastor_network_metric_cluster_time", "vitastor_network_metric_samples", ["cluster_id", "collected_at"])

def downgrade() -> None:
    op.drop_index("ix_vitastor_network_metric_cluster_time", table_name="vitastor_network_metric_samples")
    op.drop_table("vitastor_network_metric_samples")
