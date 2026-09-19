"""durable online-learner operator controls and audit

Revision ID: f0a1b2c3d4e6
Revises: e2f3a4b5c6d7
"""

from alembic import op
import sqlalchemy as sa


revision = "f0a1b2c3d4e6"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "online_learner_controls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_key", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="RUNNING"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cluster_key", "host", "metric",
            name="uq_online_learner_control_scope",
        ),
    )
    op.create_index(
        "ix_online_learner_control_scope",
        "online_learner_controls",
        ["cluster_key", "host", "metric"],
        unique=False,
    )
    op.create_table(
        "online_learner_operator_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_key", sa.String(length=128), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_online_learner_operator_audit_scope_time",
        "online_learner_operator_audits",
        ["cluster_key", "host", "metric", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_online_learner_operator_audit_action_time",
        "online_learner_operator_audits",
        ["action", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_online_learner_operator_audit_action_time",
        table_name="online_learner_operator_audits",
    )
    op.drop_index(
        "ix_online_learner_operator_audit_scope_time",
        table_name="online_learner_operator_audits",
    )
    op.drop_table("online_learner_operator_audits")
    op.drop_index(
        "ix_online_learner_control_scope",
        table_name="online_learner_controls",
    )
    op.drop_table("online_learner_controls")
