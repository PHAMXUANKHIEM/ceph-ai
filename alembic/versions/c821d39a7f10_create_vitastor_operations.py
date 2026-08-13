"""create Vitastor deploy/delete operation table

Revision ID: c821d39a7f10
Revises: ab72e9c4d105
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "c821d39a7f10"
down_revision: Union[str, Sequence[str], None] = "ab72e9c4d105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vitastor_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("cluster_id", sa.String(36), nullable=True),
        sa.Column("cluster_name", sa.String(128), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("plan_text", sa.Text(), nullable=False),
        sa.Column("progress_json", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("vitastor_operations")
