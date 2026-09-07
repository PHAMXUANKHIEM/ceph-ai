"""keep grace-period Incidents in the active deduplication boundary

Revision ID: e8f9a0b1c2d3
Revises: d3e4f5a6b7c8
"""

from alembic import op
import sqlalchemy as sa


revision = "e8f9a0b1c2d3"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


_IN_FLIGHT = (
    "status IN ('NEW','DIAGNOSING','PENDING_APPROVAL','APPROVED',"
    "'EXECUTING','GRACE_PENDING','VERIFYING')"
)


def upgrade() -> None:
    op.drop_index("uq_incidents_inflight_cluster_code", table_name="incidents")
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
    op.create_index(
        "uq_incidents_inflight_cluster_code",
        "incidents",
        [sa.text("COALESCE(cluster_id, '')"), "ceph_code"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('NEW','DIAGNOSING','PENDING_APPROVAL','APPROVED','EXECUTING','VERIFYING')"
        ),
        sqlite_where=sa.text(
            "status IN ('NEW','DIAGNOSING','PENDING_APPROVAL','APPROVED','EXECUTING','VERIFYING')"
        ),
    )
