"""Split node-resource and volume forecast state/evaluation by horizon."""

from alembic import op
import sqlalchemy as sa


revision = "m20260921forecasthorizons"
down_revision = "m20260921backupalert"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "node_resource_forecast_runs",
        sa.Column("horizon_hours", sa.Integer(), nullable=False, server_default="24"),
    )
    op.add_column(
        "node_resource_model_states",
        sa.Column("horizon_hours", sa.Integer(), nullable=False, server_default="24"),
    )
    op.drop_index("ix_node_resource_forecast_due", table_name="node_resource_forecast_runs")
    op.create_index(
        "ix_node_resource_forecast_due", "node_resource_forecast_runs",
        ["status", "target_at", "horizon_hours"], unique=False,
    )
    op.create_index(
        "ix_node_resource_forecast_scope", "node_resource_forecast_runs",
        ["cluster_name", "host", "metric", "horizon_hours"], unique=False,
    )
    op.drop_constraint("uq_node_resource_model_identity", "node_resource_model_states", type_="unique")
    op.create_unique_constraint(
        "uq_node_resource_model_identity", "node_resource_model_states",
        ["cluster_name", "host", "metric", "algorithm", "window_hours", "horizon_hours"],
    )
    op.create_index(
        "ix_node_resource_model_scope", "node_resource_model_states",
        ["cluster_name", "host", "metric", "horizon_hours"], unique=False,
    )
    op.add_column(
        "volume_forecast_runs",
        sa.Column("horizon_hours", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "volume_model_states",
        sa.Column("horizon_hours", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_index("ix_volume_forecast_due", table_name="volume_forecast_runs")
    op.drop_index("ix_volume_forecast_scope", table_name="volume_forecast_runs")
    op.create_index(
        "ix_volume_forecast_due", "volume_forecast_runs",
        ["status", "target_at", "horizon_hours"], unique=False,
    )
    op.create_index(
        "ix_volume_forecast_scope", "volume_forecast_runs",
        ["cluster_id", "pool", "image", "metric", "horizon_hours"], unique=False,
    )
    op.drop_constraint("uq_volume_model_identity", "volume_model_states", type_="unique")
    op.drop_index("ix_volume_model_scope", table_name="volume_model_states")
    op.create_unique_constraint(
        "uq_volume_model_identity", "volume_model_states",
        ["cluster_id", "pool", "image", "metric", "algorithm", "window_hours", "horizon_hours"],
    )
    op.create_index(
        "ix_volume_model_scope", "volume_model_states",
        ["cluster_id", "pool", "image", "metric", "horizon_hours"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_node_resource_model_scope", table_name="node_resource_model_states")
    op.drop_constraint("uq_node_resource_model_identity", "node_resource_model_states", type_="unique")
    op.create_unique_constraint(
        "uq_node_resource_model_identity", "node_resource_model_states",
        ["cluster_name", "host", "metric", "algorithm", "window_hours"],
    )
    op.drop_index("ix_node_resource_forecast_scope", table_name="node_resource_forecast_runs")
    op.drop_index("ix_node_resource_forecast_due", table_name="node_resource_forecast_runs")
    op.create_index(
        "ix_node_resource_forecast_due", "node_resource_forecast_runs",
        ["status", "target_at"], unique=False,
    )
    op.drop_column("node_resource_model_states", "horizon_hours")
    op.drop_column("node_resource_forecast_runs", "horizon_hours")
    op.drop_index("ix_volume_model_scope", table_name="volume_model_states")
    op.drop_constraint("uq_volume_model_identity", "volume_model_states", type_="unique")
    op.create_unique_constraint(
        "uq_volume_model_identity", "volume_model_states",
        ["cluster_id", "pool", "image", "metric", "algorithm", "window_hours"],
    )
    op.create_index(
        "ix_volume_model_scope", "volume_model_states",
        ["cluster_id", "pool", "image", "metric"], unique=False,
    )
    op.drop_index("ix_volume_forecast_scope", table_name="volume_forecast_runs")
    op.drop_index("ix_volume_forecast_due", table_name="volume_forecast_runs")
    op.create_index("ix_volume_forecast_due", "volume_forecast_runs", ["status", "target_at"], unique=False)
    op.create_index(
        "ix_volume_forecast_scope", "volume_forecast_runs",
        ["cluster_id", "pool", "image", "metric"], unique=False,
    )
    op.drop_column("volume_model_states", "horizon_hours")
    op.drop_column("volume_forecast_runs", "horizon_hours")
