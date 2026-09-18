"""persist the evidence fingerprint used for notification dedupe

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
"""

from alembic import op
import sqlalchemy as sa


revision = "f1a2b3c4d5e6"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        batch_op.add_column(
            sa.Column("last_notified_evidence_fingerprint", sa.String(length=64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        batch_op.drop_column("last_notified_evidence_fingerprint")
