"""add shadow autopilot evidence

Revision ID: d7a8b9c0e123
Revises: ce5f60718293
"""
import sqlalchemy as sa
from alembic import op

revision = "d7a8b9c0e123"
down_revision = "ce5f60718293"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("remediation_cases", sa.Column("shadow_decision", sa.String(32)))
    op.add_column("remediation_cases", sa.Column("shadow_reason", sa.Text()))
    op.add_column("remediation_cases", sa.Column("shadow_trust_score", sa.Float()))
    op.add_column("remediation_cases", sa.Column("shadow_sample_count", sa.Integer()))
    op.add_column("remediation_cases", sa.Column("shadow_recorded_at", sa.DateTime()))


def downgrade():
    op.drop_column("remediation_cases", "shadow_recorded_at")
    op.drop_column("remediation_cases", "shadow_sample_count")
    op.drop_column("remediation_cases", "shadow_trust_score")
    op.drop_column("remediation_cases", "shadow_reason")
    op.drop_column("remediation_cases", "shadow_decision")
