"""add remediation feedback audit metadata

Revision ID: d03a6e8f42b1
Revises: c92f5a7b31d4
"""

from alembic import op
import sqlalchemy as sa

revision = "d03a6e8f42b1"
down_revision = "c92f5a7b31d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("remediation_cases", sa.Column("operator_verdict_by", sa.String(64), nullable=True))
    op.add_column("remediation_cases", sa.Column("operator_verdict_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE remediation_cases SET operator_verdict_at = updated_at "
        "WHERE operator_verdict IS NOT NULL AND operator_verdict_at IS NULL"
    )
    op.create_index(
        "ix_remediation_cases_operator_verdict_at",
        "remediation_cases", ["operator_verdict_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_remediation_cases_operator_verdict_at", table_name="remediation_cases")
    op.drop_column("remediation_cases", "operator_verdict_at")
    op.drop_column("remediation_cases", "operator_verdict_by")
