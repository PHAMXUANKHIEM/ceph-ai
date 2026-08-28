"""make incident grouping safe for retention and scale

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
"""

from alembic import op


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_incidents_group_root_incident",
        "incidents",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_incidents_group_root_incident",
        "incidents",
        "incidents",
        ["group_root_incident_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_incidents_cluster_detected_at",
        "incidents",
        ["cluster_id", "detected_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_incidents_cluster_detected_at", table_name="incidents")
    op.drop_constraint(
        "fk_incidents_group_root_incident",
        "incidents",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_incidents_group_root_incident",
        "incidents",
        "incidents",
        ["group_root_incident_id"],
        ["id"],
    )
