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
    # SQLite cannot ALTER TABLE to add a foreign key; batch mode rebuilds the
    # table while preserving the existing incident rows.
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.add_column(
            sa.Column("group_root_incident_id", sa.String(length=36), nullable=True),
        )
        batch_op.create_index(
            "ix_incidents_group_root_incident_id",
            ["group_root_incident_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_incidents_group_root_incident",
            "incidents",
            ["group_root_incident_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_constraint("fk_incidents_group_root_incident", type_="foreignkey")
        batch_op.drop_index("ix_incidents_group_root_incident_id")
        batch_op.drop_column("group_root_incident_id")
