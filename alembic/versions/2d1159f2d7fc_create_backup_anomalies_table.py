"""create backup_anomalies table

Revision ID: 2d1159f2d7fc
Revises: daae81bc54e6
Create Date: 2026-07-31 09:11:05.813980

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '2d1159f2d7fc'
down_revision: Union[str, Sequence[str], None] = 'daae81bc54e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('backup_anomalies',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('backup_job_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('ai_summary', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['backup_job_id'], ['backup_jobs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    # NOTE: autogenerate also proposed dropping 'apscheduler_jobs' here —
    # REMOVED BY HAND. That table is owned/created at runtime by
    # APScheduler's SQLAlchemyJobStore (Architecture AD-11's explicit,
    # accepted exception to AD-1 "one schema owner") — it is not part of
    # this app's SQLAlchemy models on purpose, so autogenerate always sees
    # it as "extra" and proposes removing it. Alembic must never touch it.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('backup_anomalies')
