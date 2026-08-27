"""add change risk acknowledgement fingerprint

Revision ID: c92f5a7b31d4
Revises: b71e4d8a2c90
"""
from alembic import op
import sqlalchemy as sa

revision = "c92f5a7b31d4"
down_revision = "b71e4d8a2c90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("change_risk_assessments", sa.Column("assessment_hash", sa.String(length=64), nullable=True))
    op.add_column("change_risk_assessments", sa.Column("acknowledged_hash", sa.String(length=64), nullable=True))
    op.execute("UPDATE change_risk_assessments SET assessment_hash = repeat('0', 64)")
    op.alter_column("change_risk_assessments", "assessment_hash", nullable=False)


def downgrade() -> None:
    op.drop_column("change_risk_assessments", "acknowledged_hash")
    op.drop_column("change_risk_assessments", "assessment_hash")
