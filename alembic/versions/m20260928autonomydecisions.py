"""Add autonomy_decisions for off-policy learning (autonomy plan WP6.1)."""

from alembic import op
import sqlalchemy as sa


revision = "m20260928autonomydecisions"
down_revision = "m20260928incidentevidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "autonomy_decisions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), nullable=False),
        sa.Column("incident_id", sa.String(36), nullable=False),
        sa.Column("cluster_id", sa.String(36), nullable=True),
        sa.Column("fault_family", sa.String(64), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("candidates_json", sa.Text(), nullable=False),
        sa.Column("chosen_action", sa.String(64), nullable=False),
        sa.Column("chosen_by", sa.String(32), nullable=False),
        sa.Column("propensity", sa.Float(), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["remediation_cases.id"]),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", name="uq_autonomy_decisions_case"),
    )
    op.create_index("ix_autonomy_decisions_family_created", "autonomy_decisions", ["fault_family", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_autonomy_decisions_family_created", table_name="autonomy_decisions")
    op.drop_table("autonomy_decisions")
