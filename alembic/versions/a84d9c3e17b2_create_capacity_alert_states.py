"""create durable capacity alert states

Revision ID: a84d9c3e17b2
Revises: 9c17a4e82b61
"""
from alembic import op
import sqlalchemy as sa

revision = "a84d9c3e17b2"
down_revision = "9c17a4e82b61"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capacity_alert_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cluster_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=16), nullable=False),
        sa.Column("entity_name", sa.String(length=128), nullable=False),
        sa.Column("current_threshold", sa.Integer(), nullable=False),
        sa.Column("notified_threshold", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cluster_id", "entity_type", "entity_name", name="uq_capacity_alert_state_entity"),
    )


def downgrade() -> None:
    op.drop_table("capacity_alert_states")
