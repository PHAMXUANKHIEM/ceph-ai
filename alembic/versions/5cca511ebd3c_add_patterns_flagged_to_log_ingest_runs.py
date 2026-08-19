"""add patterns_flagged to log_ingest_runs

Log Intelligence L1 -- tầng triage (`watcher/log_triage.py`) ghi lại số mẫu
nó gắn cờ trong mỗi lần quét, để trả lời "cửa sổ đó có gì bất thường không"
từ bảng provenance mà không phải tính lại.

Nullable có chủ ý: 0 ("đã triage, không có gì") phải phân biệt được với
NULL ("lần quét trước khi L1 tồn tại"). Xem docstring của cột trong
shared/models.py.

Revision ID: 5cca511ebd3c
Revises: 6192ad592f06
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5cca511ebd3c'
down_revision: Union[str, Sequence[str], None] = '6192ad592f06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'log_ingest_runs',
        sa.Column('patterns_flagged', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('log_ingest_runs', 'patterns_flagged')
