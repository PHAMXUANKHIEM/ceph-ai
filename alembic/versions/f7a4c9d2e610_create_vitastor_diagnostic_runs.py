"""create Vitastor diagnostic runs

Revision ID: f7a4c9d2e610
Revises: e731fa2c8160
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f7a4c9d2e610"
down_revision: Union[str, Sequence[str], None] = "e731fa2c8160"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vitastor_diagnostic_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("health", sa.String(length=16), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("diagnosis_text", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_vitastor_diagnostic_cluster_created",
        "vitastor_diagnostic_runs", ["cluster_id", "created_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_vitastor_diagnostic_cluster_created", table_name="vitastor_diagnostic_runs")
    op.drop_table("vitastor_diagnostic_runs")
