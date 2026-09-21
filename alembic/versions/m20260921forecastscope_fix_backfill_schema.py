"""Mark unambiguous registry backfills as the explicit scope schema."""

from alembic import op
import sqlalchemy as sa


revision = "m20260921forecastscopefix"
down_revision = "m20260921forecastscope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE forecast_model_registry "
        "SET scope_schema = 'forecast-scope-v2' "
        "WHERE cluster_id IS NOT NULL AND entity_type IS NOT NULL "
        "AND entity_id IS NOT NULL AND metric IS NOT NULL "
        "AND horizon_hours IS NOT NULL"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE forecast_model_registry SET scope_schema = 'legacy-v1' "
        "WHERE cluster_id IS NOT NULL AND entity_type IS NOT NULL "
        "AND entity_id IS NOT NULL AND metric IS NOT NULL "
        "AND horizon_hours IS NOT NULL"
    ))
