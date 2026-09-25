"""Persist canonical remediation state, per-cluster autopilot mode and runtime decisions."""

from alembic import op
import sqlalchemy as sa


revision = "m20260925remediation"
down_revision = (
    "5bdecca5014e",
    "m20260921backupalert",
    "ac91e4d87210",
    "e91b6a4c2d10",
    "ad18e0f1a2b3",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clusters",
        sa.Column("autopilot_mode", sa.String(length=32), nullable=False, server_default="LEGACY"),
    )
    op.add_column(
        "actions",
        sa.Column("remediation_state", sa.String(length=32), nullable=False, server_default="PROPOSED"),
    )
    op.create_table(
        "remediation_runtime_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("cluster_id", sa.String(length=36), sa.ForeignKey("clusters.id"), nullable=True),
        sa.Column("incident_id", sa.String(length=36), sa.ForeignKey("incidents.id"), nullable=True),
        sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=False, server_default="unknown"),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("controls_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_remediation_runtime_decisions_cluster_time",
        "remediation_runtime_decisions", ["cluster_id", "created_at"], unique=False,
    )
    op.create_index(
        "ix_remediation_runtime_decisions_action_time",
        "remediation_runtime_decisions", ["action_id", "created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_remediation_runtime_decisions_action_time", table_name="remediation_runtime_decisions")
    op.drop_index("ix_remediation_runtime_decisions_cluster_time", table_name="remediation_runtime_decisions")
    op.drop_table("remediation_runtime_decisions")
    with op.batch_alter_table("actions") as batch_op:
        batch_op.drop_column("remediation_state")
    with op.batch_alter_table("clusters") as batch_op:
        batch_op.drop_column("autopilot_mode")
