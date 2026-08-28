"""store capability proposal source fingerprint

Revision ID: f27c8d1e64b3
Revises: e14b7f9c53a2
"""
from alembic import op
import sqlalchemy as sa

revision = "f27c8d1e64b3"
down_revision = "e14b7f9c53a2"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("capability_matrix_proposals", sa.Column("source_sha256", sa.String(64), nullable=True))
    op.execute("UPDATE capability_matrix_proposals SET source_sha256 = 'legacy-' || substr(id, 1, 57) WHERE source_sha256 IS NULL")
    with op.batch_alter_table("capability_matrix_proposals") as batch_op:
        batch_op.alter_column(
            "source_sha256", existing_type=sa.String(64), nullable=False,
        )

def downgrade():
    with op.batch_alter_table("capability_matrix_proposals") as batch_op:
        batch_op.drop_column("source_sha256")
