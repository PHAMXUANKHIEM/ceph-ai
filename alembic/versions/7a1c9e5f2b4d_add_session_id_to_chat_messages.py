"""add session_id to chat_messages

Revision ID: 7a1c9e5f2b4d
Revises: 2f4a8c1e6d3b
Create Date: 2026-07-22 00:00:00.000000

"""
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a1c9e5f2b4d'
down_revision: Union[str, Sequence[str], None] = '2f4a8c1e6d3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('chat_messages', sa.Column('session_id', sa.String(length=36), nullable=True))
    # Group every pre-existing row into one shared "legacy" session rather
    # than leaving session_id NULL for them — a NULL session_id would make
    # dashboard/routes/chat.py's "current session = most recent row's
    # session_id" logic misbehave the first time it runs against an
    # already-populated table (e.g. this feature landing on top of Story
    # 6.1/6.2's already-live chat history).
    legacy_session_id = str(uuid.uuid4())
    op.execute(
        sa.text("UPDATE chat_messages SET session_id = :sid WHERE session_id IS NULL").bindparams(
            sid=legacy_session_id
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('chat_messages', 'session_id')
