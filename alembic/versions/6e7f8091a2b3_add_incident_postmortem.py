"""add AI postmortem fields to incidents

Revision ID: 6e7f8091a2b3
Revises: 5d6e7f8091a2
"""

from alembic import op
import sqlalchemy as sa

revision = "6e7f8091a2b3"
down_revision = "5d6e7f8091a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.add_column(sa.Column("postmortem_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("postmortem_generated_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("postmortem_prompt_version", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_column("postmortem_prompt_version")
        batch_op.drop_column("postmortem_generated_at")
        batch_op.drop_column("postmortem_json")
