"""scope active Incident deduplication by an optional resource key

Revision ID: ab16c8d9e0f4
Revises: aa05c7e9f1d3
"""

from alembic import op
import sqlalchemy as sa


revision = "ab16c8d9e0f4"
down_revision = "aa05c7e9f1d3"
branch_labels = None
depends_on = None


_IN_FLIGHT = (
    "status IN ('NEW','DIAGNOSING','PENDING_APPROVAL','APPROVED',"
    "'EXECUTING','GRACE_PENDING','VERIFYING')"
)


def upgrade() -> None:
    op.add_column("incidents", sa.Column("dedupe_key", sa.String(length=256), nullable=True))
    op.drop_index("uq_incidents_inflight_cluster_code", table_name="incidents")
    op.create_index(
        "uq_incidents_inflight_cluster_code",
        "incidents",
        [
            sa.text("COALESCE(cluster_id, '')"),
            "ceph_code",
            sa.text("COALESCE(dedupe_key, '')"),
        ],
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
        postgresql_where=sa.text(_IN_FLIGHT),
        sqlite_where=sa.text(_IN_FLIGHT),
    )
    op.drop_column("incidents", "dedupe_key")
