"""add structured incident signal evidence

Revision ID: 7a8b9c0d1e22
Revises: 2f3a4b5c6d70
"""

from alembic import op
import sqlalchemy as sa

revision = "7a8b9c0d1e22"
down_revision = "2f3a4b5c6d70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.add_column(sa.Column("signal_evidence_json", sa.Text(), nullable=True))
    with op.batch_alter_table("log_findings") as batch_op:
        batch_op.add_column(sa.Column("correlation_evidence_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("log_findings") as batch_op:
        batch_op.drop_column("correlation_evidence_json")
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_column("signal_evidence_json")
