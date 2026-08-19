"""create capability matrix

Revision ID: 18f374b79a75
Revises: 6b5e22967d5f
Create Date: 2026-08-17
"""

from alembic import op
import sqlalchemy as sa

revision = "18f374b79a75"
down_revision = "6b5e22967d5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capability_matrix_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("command_id", sa.String(64), nullable=False),
        sa.Column("inner_command", sa.Text(), nullable=False),
        sa.Column("flag", sa.String(128), nullable=True),
        sa.Column("module", sa.String(128), nullable=True),
        sa.Column("backend", sa.String(64), nullable=True),
        sa.Column("min_major", sa.Integer(), nullable=False),
        sa.Column("max_major", sa.Integer(), nullable=True),
        sa.Column("doc_url", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.Column("verified_by", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE','DEPRECATED')",
            name="ck_capability_matrix_entries_status_valid",
        ),
    )
    op.create_index(
        "ix_capability_matrix_entries_command_id",
        "capability_matrix_entries",
        ["command_id"],
    )
    op.create_table(
        "capability_matrix_changes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "entry_id", sa.String(36), sa.ForeignKey("capability_matrix_entries.id"), nullable=False
        ),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("change_type", sa.String(16), nullable=False),
        sa.Column("entry_snapshot_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_capability_matrix_changes_entry_id",
        "capability_matrix_changes",
        ["entry_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_capability_matrix_changes_entry_id", table_name="capability_matrix_changes")
    op.drop_table("capability_matrix_changes")
    op.drop_index("ix_capability_matrix_entries_command_id", table_name="capability_matrix_entries")
    op.drop_table("capability_matrix_entries")
