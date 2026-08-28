"""group related incidents under a deterministic root

Revision ID: a7b8c9d0e1f2
Revises: f27c8d1e64b3
"""

from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "f27c8d1e64b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incidents",
        sa.Column("group_root_incident_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_incidents_group_root_incident_id",
        "incidents",
        ["group_root_incident_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_incidents_group_root_incident",
        "incidents",
        "incidents",
        ["group_root_incident_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_incidents_group_root_incident", "incidents", type_="foreignkey")
    op.drop_index("ix_incidents_group_root_incident_id", table_name="incidents")
    op.drop_column("incidents", "group_root_incident_id")
