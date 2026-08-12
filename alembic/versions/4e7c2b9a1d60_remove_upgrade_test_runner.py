"""remove upgrade test runner tables

Revision ID: 4e7c2b9a1d60
Revises: d84e2f0a7b31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4e7c2b9a1d60"
down_revision: Union[str, Sequence[str], None] = "d84e2f0a7b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("test_run_results")
    op.drop_table("test_runner_configs")


def downgrade() -> None:
    op.create_table(
        "test_runner_configs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("rgw_endpoint_zone_a", sa.String(length=255), nullable=True),
        sa.Column("rgw_endpoint_zone_b", sa.String(length=255), nullable=True),
        sa.Column("rgw_endpoint_vip", sa.String(length=255), nullable=True),
        sa.Column("test_groups", sa.Text(), nullable=True),
        sa.Column("priorities", sa.Text(), nullable=True),
        sa.Column("baseline_files", sa.Text(), nullable=True),
        sa.Column("client_host", sa.String(length=255), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "test_run_results",
        sa.Column("test_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("criteria_json", sa.Text(), nullable=True),
        sa.Column("raw_output", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("overridden", sa.Boolean(), nullable=False),
        sa.Column("override_note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("test_id"),
    )
