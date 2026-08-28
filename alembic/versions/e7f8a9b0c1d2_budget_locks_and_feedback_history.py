"""add atomic AI budget locks and append-only runbook feedback"""

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "e7f8a9b0c1d2"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_budget_locks",
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("period"),
    )
    op.bulk_insert(
        sa.table(
            "ai_budget_locks",
            sa.column("period", sa.String(length=16)),
            sa.column("period_start", sa.DateTime()),
            sa.column("updated_at", sa.DateTime()),
        ),
        [
            {"period": "daily", "period_start": datetime(1970, 1, 1), "updated_at": datetime.utcnow()},
            {"period": "monthly", "period_start": datetime(1970, 1, 1), "updated_at": datetime.utcnow()},
        ],
    )
    op.create_table(
        "ai_runbook_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("runbook_id", sa.String(length=36), nullable=False),
        sa.Column("rating", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["runbook_id"], ["ai_runbooks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_runbook_feedback_runbook_created",
        "ai_runbook_feedback",
        ["runbook_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_runbook_feedback_runbook_created", table_name="ai_runbook_feedback")
    op.drop_table("ai_runbook_feedback")
    op.drop_table("ai_budget_locks")
