"""add capability matrix AI proposals

Revision ID: e14b7f9c53a2
Revises: d03a6e8f42b1
"""
from alembic import op
import sqlalchemy as sa

revision = "e14b7f9c53a2"
down_revision = "d03a6e8f42b1"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("capability_matrix_proposals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("command_id", sa.String(64), nullable=False), sa.Column("inner_command", sa.Text(), nullable=False),
        sa.Column("min_major", sa.Integer(), nullable=False), sa.Column("max_major", sa.Integer()),
        sa.Column("doc_url", sa.Text(), nullable=False), sa.Column("evidence_excerpt", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False), sa.Column("status", sa.String(16), nullable=False),
        sa.Column("proposed_by", sa.String(64), nullable=False), sa.Column("reviewed_by", sa.String(64)),
        sa.Column("reviewed_at", sa.DateTime()), sa.Column("created_entry_id", sa.String(36), sa.ForeignKey("capability_matrix_entries.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED')", name="ck_capability_matrix_proposals_status_valid"))
    op.create_index("ix_capability_matrix_proposals_command_id", "capability_matrix_proposals", ["command_id"])
    op.create_index("ix_capability_matrix_proposals_status", "capability_matrix_proposals", ["status"])

def downgrade():
    op.drop_index("ix_capability_matrix_proposals_status", table_name="capability_matrix_proposals")
    op.drop_index("ix_capability_matrix_proposals_command_id", table_name="capability_matrix_proposals")
    op.drop_table("capability_matrix_proposals")
