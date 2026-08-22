"""correlate log findings with health incidents

Revision ID: 2f3a4b5c6d70
Revises: 8d22c1a4f901
"""

from alembic import op
import sqlalchemy as sa

revision = "2f3a4b5c6d70"
down_revision = "8d22c1a4f901"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch mode is required by SQLite, which cannot ALTER TABLE to add an
    # FK; on PostgreSQL Alembic emits ordinary ALTER statements.
    with op.batch_alter_table("log_findings") as batch_op:
        batch_op.add_column(sa.Column("correlated_incident_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("correlation_reason", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("correlated_at", sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            "fk_log_findings_correlated_incident",
            "incidents", ["correlated_incident_id"], ["id"],
        )
        batch_op.create_index(
            "ix_log_findings_correlated_incident_id", ["correlated_incident_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("log_findings") as batch_op:
        batch_op.drop_index("ix_log_findings_correlated_incident_id")
        batch_op.drop_constraint("fk_log_findings_correlated_incident", type_="foreignkey")
        batch_op.drop_column("correlated_at")
        batch_op.drop_column("correlation_reason")
        batch_op.drop_column("correlated_incident_id")
