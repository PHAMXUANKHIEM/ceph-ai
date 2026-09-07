"""create Vitastor cluster/pool/OSD capacity history

Revision ID: c5d6e7f8a9b0
Revises: b3c4d5e6f7a8
"""

from alembic import op
import sqlalchemy as sa


revision = "c5d6e7f8a9b0"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_capacity_samples",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_name", sa.String(128), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_vitastor_capacity_series", "vitastor_capacity_samples", ["cluster_id", "entity_type", "entity_name", "captured_at"])


def downgrade() -> None:
    op.drop_index("ix_vitastor_capacity_series", table_name="vitastor_capacity_samples")
    op.drop_table("vitastor_capacity_samples")
