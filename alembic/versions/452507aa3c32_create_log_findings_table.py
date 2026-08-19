"""create log findings table

Log Intelligence L2 -- kết luận của tầng phân tích AI
(`watcher/log_analysis.py`).

Mọi hàng ở đây đều đã đi qua kiểm tra phía server: `evidence_pattern_ids_json`
chỉ chứa id `log_patterns` CÓ THẬT, `affected_hosts_json` chỉ chứa host nằm
trong cấu hình, và `recommended_action_id` đã qua allowlist của
`worker/policy/action_policy.yaml` với nhóm DESTRUCTIVE bị loại tuyệt đối.
`validation_notes` ghi lại mọi lần server phải sửa/hạ cấp câu trả lời của
model. Xem docstring của model trong shared/models.py.

Revision ID: 452507aa3c32
Revises: 5cca511ebd3c
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '452507aa3c32'
down_revision: Union[str, Sequence[str], None] = '5cca511ebd3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'log_findings',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('cluster_id', sa.String(length=36), nullable=False),
        sa.Column('ingest_run_id', sa.String(length=36), nullable=False),
        sa.Column('verdict', sa.String(length=24), nullable=False),
        sa.Column('severity', sa.String(length=16), nullable=True),
        sa.Column('confidence', sa.String(length=16), nullable=True),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('root_cause_hypothesis', sa.Text(), nullable=True),
        sa.Column('evidence_pattern_ids_json', sa.Text(), nullable=True),
        sa.Column('affected_hosts_json', sa.Text(), nullable=True),
        sa.Column('affected_daemons_json', sa.Text(), nullable=True),
        sa.Column('recommended_action_id', sa.String(length=64), nullable=True),
        sa.Column('recommended_manual_steps_json', sa.Text(), nullable=True),
        sa.Column('dedupe_key', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='OPEN'),
        sa.Column('model_name', sa.String(length=64), nullable=True),
        sa.Column('prompt_version', sa.String(length=16), nullable=True),
        sa.Column('validation_notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['cluster_id'], ['clusters.id']),
        sa.ForeignKeyConstraint(['ingest_run_id'], ['log_ingest_runs.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint(
            "verdict IN ('FINDING','NO_FINDING','INSUFFICIENT_EVIDENCE')",
            name='ck_log_findings_verdict_valid',
        ),
        sa.CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','RESOLVED')",
            name='ck_log_findings_status_valid',
        ),
    )
    op.create_index('ix_log_findings_cluster_id', 'log_findings', ['cluster_id'])
    op.create_index('ix_log_findings_ingest_run_id', 'log_findings', ['ingest_run_id'])
    op.create_index('ix_log_findings_dedupe_key', 'log_findings', ['dedupe_key'])
    op.create_index('ix_log_findings_created_at', 'log_findings', ['created_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('log_findings')
