"""Add explicit, migration-safe dimensions to forecast model registry."""

from alembic import op
import sqlalchemy as sa


revision = "m20260921forecastscope"
down_revision = "m20260921rgwmetrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, column in (
        ("scope_schema", sa.Column("scope_schema", sa.String(length=32), nullable=True)),
        ("cluster_id", sa.Column("cluster_id", sa.String(length=255), nullable=True)),
        ("entity_type", sa.Column("entity_type", sa.String(length=32), nullable=True)),
        ("entity_id", sa.Column("entity_id", sa.String(length=255), nullable=True)),
        ("host", sa.Column("host", sa.String(length=255), nullable=True)),
        ("metric", sa.Column("metric", sa.String(length=64), nullable=True)),
        ("horizon_hours", sa.Column("horizon_hours", sa.Integer(), nullable=True)),
    ):
        op.add_column("forecast_model_registry", column)
    op.create_index(
        "ix_forecast_model_registry_dimensions",
        "forecast_model_registry",
        ["cluster_id", "entity_type", "entity_id", "metric", "horizon_hours"],
        unique=False,
    )

    # Backfill only unambiguous legacy keys.  Rows that cannot be parsed stay
    # nullable and are therefore ineligible for promotion until reviewed.
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, scope_type, scope_key, training_window_hours "
        "FROM forecast_model_registry"
    )).mappings()
    for row in rows:
        parts = [part.strip() for part in str(row["scope_key"] or "").split("|")]
        values = {"scope_schema": "legacy-v1"}
        if row["scope_type"] == "NODE_RESOURCE" and len(parts) == 3:
            values["scope_schema"] = "forecast-scope-v2"
            values.update(
                cluster_id=parts[0], entity_type="node", entity_id=parts[1],
                host=parts[1], metric=parts[2], horizon_hours=row["training_window_hours"],
            )
        elif row["scope_type"] == "VOLUME" and len(parts) == 4:
            values["scope_schema"] = "forecast-scope-v2"
            values.update(
                cluster_id=parts[0], entity_type="volume", entity_id=f"{parts[1]}/{parts[2]}",
                metric=parts[3], horizon_hours=row["training_window_hours"],
            )
        assignments = ", ".join(f"{key} = :{key}" for key in values)
        values["id"] = row["id"]
        bind.execute(sa.text(
            f"UPDATE forecast_model_registry SET {assignments} WHERE id = :id"
        ), values)


def downgrade() -> None:
    op.drop_index("ix_forecast_model_registry_dimensions", table_name="forecast_model_registry")
    for name in ("horizon_hours", "metric", "host", "entity_id", "entity_type", "cluster_id", "scope_schema"):
        op.drop_column("forecast_model_registry", name)
