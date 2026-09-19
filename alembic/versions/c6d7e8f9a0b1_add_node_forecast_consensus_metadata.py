"""store node forecast model votes, residuals and intervals

Revision ID: c6d7e8f9a0b1
Revises: b4c5d6e7f8a9
"""

from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f9a0b1"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("consensus_status", sa.String(length=32)),
    ("consensus_ratio", sa.Float()),
    ("consensus_candidate_count", sa.Integer()),
    ("predicted_low", sa.Float()),
    ("predicted_high", sa.Float()),
    ("anomaly_score", sa.Float()),
)


def upgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_runs") as batch_op:
        for name, column_type in _COLUMNS:
            batch_op.add_column(sa.Column(name, column_type, nullable=True))
        batch_op.add_column(sa.Column("residual_percent", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("model_votes_json", sa.Text(), nullable=True))
    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        for name, column_type in _COLUMNS:
            if name == "consensus_candidate_count":
                batch_op.add_column(sa.Column(name, column_type, nullable=True))
            else:
                batch_op.add_column(sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        for name, _column_type in reversed(_COLUMNS):
            batch_op.drop_column(name)
    with op.batch_alter_table("node_resource_forecast_runs") as batch_op:
        batch_op.drop_column("model_votes_json")
        batch_op.drop_column("residual_percent")
        for name, _column_type in reversed(_COLUMNS):
            batch_op.drop_column(name)
