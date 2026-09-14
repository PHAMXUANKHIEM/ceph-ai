"""store sampled RBD data-object mappings

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("volume_osd_mappings", sa.Column(
        "pgids_json", sa.Text(), nullable=False, server_default=sa.text("'[]'"),
    ))
    op.add_column("volume_osd_mappings", sa.Column(
        "sampled_objects_json", sa.Text(), nullable=False, server_default=sa.text("'[]'"),
    ))
    op.add_column("volume_osd_mappings", sa.Column(
        "data_object_count", sa.Integer(), nullable=False, server_default="0",
    ))
    # Existing rows were produced from rbd_header and must not be treated as
    # data placement evidence until the new collector refreshes them.
    op.add_column("volume_osd_mappings", sa.Column(
        "mapping_scope", sa.String(length=32), nullable=False, server_default="header_legacy",
    ))
    with op.batch_alter_table("volume_osd_mappings") as batch_op:
        batch_op.alter_column("pgids_json", server_default=None)
        batch_op.alter_column("sampled_objects_json", server_default=None)
        batch_op.alter_column("data_object_count", server_default=None)
        batch_op.alter_column("mapping_scope", server_default=None)


def downgrade() -> None:
    op.drop_column("volume_osd_mappings", "mapping_scope")
    op.drop_column("volume_osd_mappings", "data_object_count")
    op.drop_column("volume_osd_mappings", "sampled_objects_json")
    op.drop_column("volume_osd_mappings", "pgids_json")
