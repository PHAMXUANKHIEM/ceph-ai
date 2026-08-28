"""split telegram_message_id into per-channel telegram_message_ids

Revision ID: 6b1f3a9d7e2c
Revises: 91d7e9723457
Create Date: 2026-08-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '6b1f3a9d7e2c'
down_revision: Union[str, Sequence[str], None] = '91d7e9723457'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('actions', sa.Column('telegram_message_ids', sa.Text(), nullable=True))
    # Preserve the legacy single-channel message so an upgrade does not make
    # already-sent approval messages impossible to edit.  The old schema did
    # not retain the channel name, so incident is the only safe legacy key.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "UPDATE actions SET telegram_message_ids = "
            "json_build_object('incident', telegram_message_id)::text "
            "WHERE telegram_message_id IS NOT NULL"
        ))
    else:
        op.execute(sa.text(
            "UPDATE actions SET telegram_message_ids = "
            "'{\"incident\":' || CAST(telegram_message_id AS TEXT) || '}' "
            "WHERE telegram_message_id IS NOT NULL"
        ))
    # SQLite cannot execute ALTER TABLE ... DROP COLUMN on older versions;
    # Alembic's batch implementation rebuilds the table when necessary and
    # emits a normal DROP COLUMN on databases that support it.
    with op.batch_alter_table('actions') as batch_op:
        batch_op.drop_column('telegram_message_id')


def downgrade() -> None:
    op.add_column('actions', sa.Column('telegram_message_id', sa.Integer(), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            "UPDATE actions SET telegram_message_id = "
            "NULLIF(telegram_message_ids::json ->> 'incident', '')::integer "
            "WHERE telegram_message_ids IS NOT NULL"
        ))
    else:
        op.execute(sa.text(
            "UPDATE actions SET telegram_message_id = "
            "CAST(json_extract(telegram_message_ids, '$.incident') AS INTEGER) "
            "WHERE telegram_message_ids IS NOT NULL"
        ))
    with op.batch_alter_table('actions') as batch_op:
        batch_op.drop_column('telegram_message_ids')
