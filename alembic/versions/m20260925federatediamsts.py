"""Add RGW federated STS session registry."""

from alembic import op
import sqlalchemy as sa


revision = "m20260925federatediamsts"
down_revision = "m20260925federatediamreconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rgw_federated_sts_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider_id", sa.String(36), sa.ForeignKey("rgw_federated_identity_providers.id"), nullable=False),
        sa.Column("mapping_id", sa.String(36), sa.ForeignKey("rgw_federated_role_mappings.id"), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("role_name", sa.String(128), nullable=False),
        sa.Column("session_name", sa.String(64), nullable=False),
        sa.Column("subject_fingerprint", sa.String(64), nullable=False),
        sa.Column("session_tags_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(24), nullable=False, server_default="REQUESTED"),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("access_key_id", sa.String(128), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_rgw_federated_sts_session_status_expiry",
        "rgw_federated_sts_sessions", ["status", "expires_at"],
    )
    op.create_index(
        "ix_rgw_federated_sts_session_provider_created",
        "rgw_federated_sts_sessions", ["provider_id", "created_at"],
    )
    op.create_index(
        "ix_rgw_federated_sts_session_mapping_created",
        "rgw_federated_sts_sessions", ["mapping_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rgw_federated_sts_session_mapping_created", table_name="rgw_federated_sts_sessions")
    op.drop_index("ix_rgw_federated_sts_session_provider_created", table_name="rgw_federated_sts_sessions")
    op.drop_index("ix_rgw_federated_sts_session_status_expiry", table_name="rgw_federated_sts_sessions")
    op.drop_table("rgw_federated_sts_sessions")
