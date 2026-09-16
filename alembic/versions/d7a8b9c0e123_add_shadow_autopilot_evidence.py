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
    with op.batch_alter_table("remediation_cases") as batch_op:
        batch_op.drop_column("shadow_recorded_at")
    with op.batch_alter_table("remediation_cases") as batch_op:
        batch_op.drop_column("shadow_sample_count")
    with op.batch_alter_table("remediation_cases") as batch_op:
        batch_op.drop_column("shadow_trust_score")
    with op.batch_alter_table("remediation_cases") as batch_op:
        batch_op.drop_column("shadow_reason")
    with op.batch_alter_table("remediation_cases") as batch_op:
        batch_op.drop_column("shadow_decision")
