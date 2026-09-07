"""create normalized Vitastor Prometheus samples

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
"""

from alembic import op
import sqlalchemy as sa


revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_prometheus_samples",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("metric_name", sa.String(255), nullable=False),
        sa.Column("labels_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vitastor_prometheus_cluster_metric_time", "vitastor_prometheus_samples", ["cluster_id", "metric_name", "collected_at"])


def downgrade() -> None:
    op.drop_index("ix_vitastor_prometheus_cluster_metric_time", table_name="vitastor_prometheus_samples")
    op.drop_table("vitastor_prometheus_samples")
