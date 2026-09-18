"""add rolling outcome metrics for node and volume model states

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""

from alembic import op
import sqlalchemy as sa


revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


_COLUMNS = (
    sa.Column("rolling_sample_count", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("rolling_mae", sa.Float(), nullable=True),
    sa.Column("rolling_rmse", sa.Float(), nullable=True),
    sa.Column("rolling_smape", sa.Float(), nullable=True),
    sa.Column("rolling_bias", sa.Float(), nullable=True),
    sa.Column("rolling_metrics_json", sa.Text(), nullable=True),
)


def upgrade() -> None:
    for table in ("node_resource_model_states", "volume_model_states"):
        with op.batch_alter_table(table) as batch_op:
            for column in _COLUMNS:
                batch_op.add_column(column.copy())


def downgrade() -> None:
    for table in ("volume_model_states", "node_resource_model_states"):
        with op.batch_alter_table(table) as batch_op:
            for column in reversed(_COLUMNS):
                batch_op.drop_column(column.name)
