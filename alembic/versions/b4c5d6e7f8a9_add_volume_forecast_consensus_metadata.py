"""store volume forecast consensus provenance

Revision ID: b4c5d6e7f8a9
Revises: ac17d9e0f5a0
"""

from alembic import op
import sqlalchemy as sa


revision = "b4c5d6e7f8a9"
down_revision = "ac17d9e0f5a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("volume_early_forecasts") as batch_op:
        batch_op.add_column(sa.Column("consensus_status", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("consensus_ratio", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("consensus_candidate_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("predicted_low", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("predicted_high", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("model_votes_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("volume_early_forecasts") as batch_op:
        batch_op.drop_column("model_votes_json")
        batch_op.drop_column("predicted_high")
        batch_op.drop_column("predicted_low")
        batch_op.drop_column("consensus_candidate_count")
        batch_op.drop_column("consensus_ratio")
        batch_op.drop_column("consensus_status")
