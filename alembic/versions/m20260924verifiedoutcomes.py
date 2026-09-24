"""Record provenance for independently verified online-learning outcomes."""

from alembic import op
import sqlalchemy as sa


revision = "m20260924verifiedoutcomes"
down_revision = "m20260923rbdcapacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("online_learner_labels") as batch:
        batch.add_column(sa.Column("evidence_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("source_model_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("outcome_observed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("source_incident_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("source_action_id", sa.String(36), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("online_learner_labels") as batch:
        batch.drop_column("source_action_id")
        batch.drop_column("source_incident_id")
        batch.drop_column("outcome_observed_at")
        batch.drop_column("source_model_version")
        batch.drop_column("evidence_fingerprint")
