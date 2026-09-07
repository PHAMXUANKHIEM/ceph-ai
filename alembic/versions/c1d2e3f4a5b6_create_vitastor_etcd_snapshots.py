"""create Vitastor Etcd snapshot metadata

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_etcd_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="FAILED"),
        sa.Column("destination", sa.Text(), nullable=False, server_default=""),
        sa.Column("revision", sa.BigInteger(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vitastor_etcd_snapshot_cluster_created", "vitastor_etcd_snapshots", ["cluster_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_vitastor_etcd_snapshot_cluster_created", table_name="vitastor_etcd_snapshots")
    op.drop_table("vitastor_etcd_snapshots")
