"""create durable JSON state for bounded online learners

Revision ID: c1d2e3f4a5b7
Revises: b5c6d7e8f9a0
"""

from alembic import op
import sqlalchemy as sa


revision = "c1d2e3f4a5b7"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "online_learner_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_key", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("algorithm", sa.String(length=64), nullable=False),
        sa.Column("feature_schema", sa.String(length=64), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("state_checksum", sa.String(length=64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_learned_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cluster_key", "host", "metric", "model_version",
            name="uq_online_learner_state_identity",
        ),
    )
    op.create_index(
        "ix_online_learner_state_lookup",
        "online_learner_states", ["cluster_key", "host", "metric"],
    )


def downgrade() -> None:
    op.drop_index("ix_online_learner_state_lookup", table_name="online_learner_states")
    op.drop_table("online_learner_states")
