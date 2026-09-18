"""add predictive alert evidence fingerprint and suppression window

Revision ID: e0f1a2b3c4d5
Revises: d7e8f9a0b1c2
"""

from alembic import op
import sqlalchemy as sa


revision = "e0f1a2b3c4d5"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        batch_op.add_column(sa.Column("evidence_fingerprint", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("suppressed_until", sa.DateTime(), nullable=True))
        batch_op.create_index(
            "ix_node_resource_forecast_alerts_evidence_fingerprint",
            ["evidence_fingerprint"],
        )


def downgrade() -> None:
    with op.batch_alter_table("node_resource_forecast_alerts") as batch_op:
        batch_op.drop_index("ix_node_resource_forecast_alerts_evidence_fingerprint")
        batch_op.drop_column("suppressed_until")
        batch_op.drop_column("evidence_fingerprint")
