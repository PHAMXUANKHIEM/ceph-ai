"""Add quality dimensions to persisted block-storage telemetry."""

from alembic import op
import sqlalchemy as sa


revision = "m20260925metricquality"
down_revision = "m20260925merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("volume_metrics") as batch:
        batch.add_column(sa.Column("read_bytes_per_sec", sa.Float(), nullable=True))
        batch.add_column(sa.Column("write_bytes_per_sec", sa.Float(), nullable=True))
        batch.add_column(sa.Column("queue_depth", sa.Float(), nullable=True))
        batch.add_column(sa.Column("p95_latency_ms", sa.Float(), nullable=True))
        batch.add_column(sa.Column("freshness_seconds", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("volume_metrics") as batch:
        batch.drop_column("freshness_seconds")
        batch.drop_column("p95_latency_ms")
        batch.drop_column("queue_depth")
        batch.drop_column("write_bytes_per_sec")
        batch.drop_column("read_bytes_per_sec")
