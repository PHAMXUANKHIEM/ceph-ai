"""scope CRUSH snapshots and distributions by cluster

Revision ID: d8f3a1c6b902
Revises: c7d4e5f6a701
Create Date: 2026-08-15 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision: str = "d8f3a1c6b902"
down_revision: str = "c7d4e5f6a701"
branch_labels = None
depends_on = None


def _primary_key_name(table_name: str) -> str:
    """Return the PK name that exists in the target database.

    The table's original migration created an unnamed PrimaryKeyConstraint.
    PostgreSQL therefore named it ``<table>_pkey`` while SQLite reflection
    reports no name.  Do not assume Alembic's later naming convention was
    present when that older constraint was created.
    """
    reflected_name = sa.inspect(op.get_bind()).get_pk_constraint(table_name).get("name")
    return reflected_name or f"pk_{table_name}"


def upgrade() -> None:
    op.add_column("crush_structure_snapshots", sa.Column("cluster_id", sa.String(36), nullable=True))
    op.add_column("crush_osd_distribution", sa.Column("cluster_id", sa.String(36), nullable=True))

    # All pre-migration CRUSH rows came from the settings-backed default
    # cluster. Preserve them by assigning that cluster before enforcing the
    # new scope.
    op.execute(sa.text(
        "UPDATE crush_structure_snapshots SET cluster_id = "
        "(SELECT id FROM clusters WHERE is_default = true LIMIT 1)"
    ))
    op.execute(sa.text(
        "UPDATE crush_osd_distribution SET cluster_id = "
        "(SELECT id FROM clusters WHERE is_default = true LIMIT 1)"
    ))

    with op.batch_alter_table("crush_structure_snapshots") as batch_op:
        batch_op.alter_column("cluster_id", existing_type=sa.String(36), nullable=False)
        batch_op.create_foreign_key(
            "fk_crush_structure_snapshots_cluster_id", "clusters", ["cluster_id"], ["id"]
        )
    op.create_index(
        "ix_crush_structure_snapshots_cluster_id",
        "crush_structure_snapshots",
        ["cluster_id"],
    )

    distribution_pk_name = _primary_key_name("crush_osd_distribution")
    with op.batch_alter_table(
        "crush_osd_distribution",
        naming_convention={"pk": "pk_%(table_name)s"},
    ) as batch_op:
        batch_op.alter_column("cluster_id", existing_type=sa.String(36), nullable=False)
        batch_op.create_foreign_key(
            "fk_crush_osd_distribution_cluster_id", "clusters", ["cluster_id"], ["id"]
        )
        batch_op.drop_constraint(distribution_pk_name, type_="primary")
        batch_op.create_primary_key(
            "pk_crush_osd_distribution", ["cluster_id", "osd_id"]
        )


def downgrade() -> None:
    # Downgrade cannot represent duplicate OSD ids from multiple clusters;
    # retain the default cluster's rows and discard secondary scoped rows.
    op.execute(sa.text(
        "DELETE FROM crush_osd_distribution WHERE cluster_id != "
        "(SELECT id FROM clusters WHERE is_default = true LIMIT 1)"
    ))
    distribution_pk_name = _primary_key_name("crush_osd_distribution")
    with op.batch_alter_table(
        "crush_osd_distribution",
        naming_convention={"pk": "pk_%(table_name)s"},
    ) as batch_op:
        batch_op.drop_constraint("fk_crush_osd_distribution_cluster_id", type_="foreignkey")
        batch_op.drop_constraint(distribution_pk_name, type_="primary")
        batch_op.create_primary_key("pk_crush_osd_distribution", ["osd_id"])
        batch_op.drop_column("cluster_id")

    op.drop_index("ix_crush_structure_snapshots_cluster_id", table_name="crush_structure_snapshots")
    with op.batch_alter_table("crush_structure_snapshots") as batch_op:
        batch_op.drop_constraint("fk_crush_structure_snapshots_cluster_id", type_="foreignkey")
        batch_op.drop_column("cluster_id")
