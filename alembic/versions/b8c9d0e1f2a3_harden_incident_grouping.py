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
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_constraint(
            "fk_incidents_group_root_incident", type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_incidents_group_root_incident",
            "incidents",
            ["group_root_incident_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_incidents_cluster_detected_at",
            ["cluster_id", "detected_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_index("ix_incidents_cluster_detected_at")
        batch_op.drop_constraint(
            "fk_incidents_group_root_incident", type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_incidents_group_root_incident",
            "incidents",
            ["group_root_incident_id"],
            ["id"],
        )
