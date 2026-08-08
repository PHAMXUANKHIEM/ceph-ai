"""merge crush osd distribution and clusters heads

Revision ID: c4a7f0e2b6d1
Revises: be5e3bfbfac1, b1a5a9d6b8b5
Create Date: 2026-08-07 23:10:00.000000

Story 12.1's crush_structure_snapshots/crush_osd_distribution migrations
and the multi-cluster observability feature's clusters-table migration
were developed concurrently, both branching off the same parent revision
(6b1f3a9d7e2c) -- leaving `alembic upgrade head` ambiguous (2 heads). This
merge point is a no-op (both branches' own upgrade()/downgrade() already
did their real work) purely to make "head" unambiguous again.
"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = 'c4a7f0e2b6d1'
down_revision: Union[str, Sequence[str], None] = ('be5e3bfbfac1', 'b1a5a9d6b8b5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op merge — both parent branches already created their own tables."""
    pass


def downgrade() -> None:
    """No-op merge."""
    pass
