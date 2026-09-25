"""Create the validated RGW federated role-mapping registry."""

from alembic import op
import sqlalchemy as sa


revision = "m20260924federatediammapping"
down_revision = "m20260924federatediam"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rgw_federated_role_mappings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("provider_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_key", sa.String(128), nullable=False),
        sa.Column("match_value", sa.String(255), nullable=False),
        sa.Column("policy_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_rgw_federated_role_mapping_name"),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["rgw_federated_identity_providers.id"],
        ),
    )
    op.create_index(
        "ix_rgw_federated_role_mapping_provider_status",
        "rgw_federated_role_mappings",
        ["provider_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rgw_federated_role_mapping_provider_status",
        table_name="rgw_federated_role_mappings",
    )
    op.drop_table("rgw_federated_role_mappings")
