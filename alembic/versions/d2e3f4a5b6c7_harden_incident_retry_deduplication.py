"""make active Ceph Incident creation atomic

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6, f7a8b9c0d1e2
"""

from alembic import op
import sqlalchemy as sa


revision = "d2e3f4a5b6c7"
# The previously lost Vitastor migration branch and the tracked main branch
# had diverged.  This revision deliberately merges them, then adds the
# Incident invariant used by both Watcher paths.
down_revision = ("c1d2e3f4a5b6", "f7a8b9c0d1e2")
branch_labels = None
depends_on = None


_IN_FLIGHT = "status IN ('NEW','DIAGNOSING','PENDING_APPROVAL','APPROVED','EXECUTING','VERIFYING')"


def upgrade() -> None:
    op.create_index(
        "uq_incidents_inflight_cluster_code",
        "incidents",
        [sa.text("COALESCE(cluster_id, '')"), "ceph_code"],
        unique=True,
        postgresql_where=sa.text(_IN_FLIGHT),
        sqlite_where=sa.text(_IN_FLIGHT),
    )


def downgrade() -> None:
    op.drop_index("uq_incidents_inflight_cluster_code", table_name="incidents")
