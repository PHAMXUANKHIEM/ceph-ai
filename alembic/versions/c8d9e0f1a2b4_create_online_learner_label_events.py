"""Create append-only audit events for online-learning label policy.

Revision ID: c8d9e0f1a2b4
Revises: b8c9d0e1f2a4
"""

from alembic import op
import sqlalchemy as sa


revision = "c8d9e0f1a2b4"
down_revision = "b8c9d0e1f2a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "online_learner_label_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("label_id", sa.String(length=36), nullable=True),
        sa.Column("source_run_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["label_id"], ["online_learner_labels.id"]),
        sa.ForeignKeyConstraint(["source_run_id"], ["node_resource_forecast_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_online_learner_label_event_source_action",
        "online_learner_label_events",
        ["source_run_id", "action"],
    )
    op.create_index(
        "ix_online_learner_label_event_created_at",
        "online_learner_label_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_online_learner_label_event_created_at", table_name="online_learner_label_events")
    op.drop_index("ix_online_learner_label_event_source_action", table_name="online_learner_label_events")
    op.drop_table("online_learner_label_events")
