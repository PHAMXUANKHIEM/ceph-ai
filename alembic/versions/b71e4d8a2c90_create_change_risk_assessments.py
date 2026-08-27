"""create change risk assessments

Revision ID: b71e4d8a2c90
Revises: a84d9c3e17b2
"""
from alembic import op
import sqlalchemy as sa

revision = "b71e4d8a2c90"
down_revision = "a84d9c3e17b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "change_risk_assessments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("action_id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=True),
        sa.Column("risk_level", sa.String(length=24), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("regression_count", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["action_id"], ["actions.id"]),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_id"),
    )


def downgrade() -> None:
    op.drop_table("change_risk_assessments")
