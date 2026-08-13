"""normalize historical RBD latency counters to milliseconds

Revision ID: 82c4d1e9a6f0
Revises: 7a3f91c8e2b4
"""

from typing import Sequence, Union

from alembic import op


revision: str = "82c4d1e9a6f0"
down_revision: Union[str, Sequence[str], None] = "7a3f91c8e2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Before this revision native nanosecond counters were stored in columns
    # named *_ms. Values >=10 seconds are the unmistakably corrupted part of
    # the history (e.g. 10,713,909 displayed as ms instead of 10.71 ms).
    # The guard avoids changing legitimate values already supplied through an
    # explicit *_latency_ms adapter.
    op.execute(
        "UPDATE volume_metrics SET read_latency_ms = read_latency_ms / 1000000.0 "
        "WHERE read_latency_ms >= 10000"
    )
    op.execute(
        "UPDATE volume_metrics SET write_latency_ms = write_latency_ms / 1000000.0 "
        "WHERE write_latency_ms >= 10000"
    )


def downgrade() -> None:
    # The thresholded repair cannot be reversed safely: after conversion its
    # rows are indistinguishable from genuine millisecond measurements.
    pass
