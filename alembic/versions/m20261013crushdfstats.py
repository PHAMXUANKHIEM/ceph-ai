"""Persist extended per-OSD ceph osd df metrics."""

from alembic import op
import sqlalchemy as sa


revision = "m20261013crushdfstats"
down_revision = "m20261002bluestoreslowops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("crush_osd_distribution", sa.Column("stats_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("crush_osd_distribution", "stats_json")
