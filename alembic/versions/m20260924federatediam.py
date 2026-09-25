"""Create the secret-free RGW federated IAM provider registry."""

from alembic import op
import sqlalchemy as sa


revision = "m20260924federatediam"
down_revision = "m20260924verifiedoutcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rgw_federated_identity_providers",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("provider_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="DRAFT"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("issuer_url", sa.Text(), nullable=True),
        sa.Column("endpoint_url", sa.Text(), nullable=True),
        sa.Column("audience", sa.String(255), nullable=True),
        sa.Column("secret_ref", sa.String(255), nullable=True),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_rgw_federated_identity_provider_name"),
    )
    op.create_index(
        "ix_rgw_federated_identity_provider_status",
        "rgw_federated_identity_providers",
        ["status", "provider_type"],
    )
    op.create_table(
        "rgw_federated_identity_audits",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("provider_id", sa.String(36), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("result", sa.String(24), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["rgw_federated_identity_providers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rgw_federated_identity_audit_provider_time",
        "rgw_federated_identity_audits",
        ["provider_id", "created_at"],
    )
    op.create_index(
        "ix_rgw_federated_identity_audit_result",
        "rgw_federated_identity_audits",
        ["result", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rgw_federated_identity_audit_result", table_name="rgw_federated_identity_audits")
    op.drop_index("ix_rgw_federated_identity_audit_provider_time", table_name="rgw_federated_identity_audits")
    op.drop_table("rgw_federated_identity_audits")
    op.drop_index("ix_rgw_federated_identity_provider_status", table_name="rgw_federated_identity_providers")
    op.drop_table("rgw_federated_identity_providers")
