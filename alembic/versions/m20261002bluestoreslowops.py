"""Add bluestore_slow_op_samples for the BlueStore slow-op gate (autonomy plan WP1.2)."""

from alembic import op
import sqlalchemy as sa


revision = "m20261002bluestoreslowops"
down_revision = "m20260928shadowpolicy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bluestore_slow_op_samples",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("cluster_id", sa.String(36), nullable=True),
        sa.Column("sampled_at", sa.DateTime(), nullable=False),
        sa.Column("osd_count", sa.Integer(), nullable=False),
        sa.Column("osd_ids_json", sa.Text(), nullable=False),
        sa.Column("osd_hosts_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bluestore_slow_op_samples_cluster_sampled",
        "bluestore_slow_op_samples",
        ["cluster_id", "sampled_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_bluestore_slow_op_samples_cluster_sampled", table_name="bluestore_slow_op_samples")
    op.drop_table("bluestore_slow_op_samples")
