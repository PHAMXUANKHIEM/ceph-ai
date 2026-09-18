"""create verified online learner label queue

Revision ID: f6a7b8c9d0e1
Revises: e4f5a6b7c8d9
"""

from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "online_learner_labels",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_key", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("sample_id", sa.String(length=255), nullable=False),
        sa.Column("source_run_id", sa.String(length=36), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("label_value", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="READY"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_run_id"], ["node_resource_forecast_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_run_id", name="uq_online_learner_label_source_run"),
        sa.UniqueConstraint(
            "cluster_key", "host", "metric", "sample_id",
            name="uq_online_learner_label_sample",
        ),
    )
    op.create_index(
        "ix_online_learner_label_queue",
        "online_learner_labels", ["status", "cluster_key", "host", "metric"],
    )


def downgrade() -> None:
    op.drop_index("ix_online_learner_label_queue", table_name="online_learner_labels")
    op.drop_table("online_learner_labels")
