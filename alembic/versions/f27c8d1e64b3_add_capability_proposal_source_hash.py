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
    op.alter_column("capability_matrix_proposals", "source_sha256", nullable=False)

def downgrade():
    op.drop_column("capability_matrix_proposals", "source_sha256")
