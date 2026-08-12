"""merge AI persona and removed test runner migration heads

Revision ID: 7a3f91c8e2b4
Revises: 4e7c2b9a1d60, e91b6a4c2d10
"""

from typing import Sequence, Union


revision: str = "7a3f91c8e2b4"
down_revision: Union[str, Sequence[str], None] = ("4e7c2b9a1d60", "e91b6a4c2d10")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
