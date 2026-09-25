"""Add per-user, per-cluster capability grants and Single Full audit."""

from alembic import op
import sqlalchemy as sa


revision = "m20260925clusteraccess"
down_revision = "m20260925federatediamsts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cluster_capability_grants",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("capability", sa.String(96), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("granted_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "cluster_id", "capability",
            name="uq_cluster_capability_grant_user_cluster_capability",
        ),
    )
    op.create_index(
        "ix_cluster_capability_grants_user_cluster",
        "cluster_capability_grants", ["user_id", "cluster_id"],
    )
    op.create_index(
        "ix_cluster_capability_grants_cluster_capability",
        "cluster_capability_grants", ["cluster_id", "capability"],
    )

    op.create_table(
        "single_full_audits",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("telegram_chat_id", sa.String(128), nullable=True),
        sa.Column("cluster_id", sa.String(36), nullable=False),
        sa.Column("cluster_ref", sa.String(256), nullable=False),
        sa.Column("prompt_sha256", sa.String(64), nullable=False),
        sa.Column("command_class", sa.String(64), nullable=False, server_default="single_full"),
        sa.Column("operator_acknowledged", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(24), nullable=False, server_default="RUNNING"),
        sa.Column("result_code", sa.String(64), nullable=True),
        sa.Column("error_type", sa.String(128), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_single_full_audit_run_id"),
    )
    op.create_index(
        "ix_single_full_audits_cluster_created",
        "single_full_audits", ["cluster_id", "created_at"],
    )
    op.create_index(
        "ix_single_full_audits_actor_created",
        "single_full_audits", ["actor", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_single_full_audits_actor_created", table_name="single_full_audits")
    op.drop_index("ix_single_full_audits_cluster_created", table_name="single_full_audits")
    op.drop_table("single_full_audits")
    op.drop_index("ix_cluster_capability_grants_cluster_capability", table_name="cluster_capability_grants")
    op.drop_index("ix_cluster_capability_grants_user_cluster", table_name="cluster_capability_grants")
    op.drop_table("cluster_capability_grants")
