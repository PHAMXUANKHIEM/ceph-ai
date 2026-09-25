"""Add Worker reconciliation state to federated role mappings."""

from alembic import op
import sqlalchemy as sa


revision = "m20260925federatediamreconcile"
down_revision = "m20260924federatediammapping"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rgw_federated_role_mappings",
        sa.Column("rgw_role_name", sa.String(128), nullable=True),
    )
    op.add_column(
        "rgw_federated_role_mappings",
        sa.Column("reconcile_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "rgw_federated_role_mappings",
        sa.Column("last_reconciled_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "rgw_federated_role_mappings",
        sa.Column("last_reconcile_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rgw_federated_role_mappings", "last_reconcile_error")
    op.drop_column("rgw_federated_role_mappings", "last_reconciled_at")
    op.drop_column("rgw_federated_role_mappings", "reconcile_attempts")
    op.drop_column("rgw_federated_role_mappings", "rgw_role_name")
