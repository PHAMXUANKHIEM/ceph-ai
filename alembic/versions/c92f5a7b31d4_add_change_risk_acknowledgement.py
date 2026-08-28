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
    # Use a portable literal instead of PostgreSQL-only repeat().
    op.execute("UPDATE change_risk_assessments SET assessment_hash = '0000000000000000000000000000000000000000000000000000000000000000'")
    # SQLite applies NOT NULL changes by rebuilding the table.
    with op.batch_alter_table("change_risk_assessments") as batch_op:
        batch_op.alter_column(
            "assessment_hash", existing_type=sa.String(length=64), nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("change_risk_assessments") as batch_op:
        batch_op.drop_column("acknowledged_hash")
        batch_op.drop_column("assessment_hash")
