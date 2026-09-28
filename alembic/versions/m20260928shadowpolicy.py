"""Store the shadow execute/escalate recommendation on autonomy_decisions (WP6.3)."""

from alembic import op
import sqlalchemy as sa


revision = "m20260928shadowpolicy"
down_revision = "m20260928autonomydecisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("autonomy_decisions", sa.Column("shadow_recommendation", sa.String(16), nullable=True))
    op.add_column("autonomy_decisions", sa.Column("shadow_reasons_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("autonomy_decisions", "shadow_reasons_json")
    op.drop_column("autonomy_decisions", "shadow_recommendation")
