"""scope chat messages by Ceph cluster

Revision ID: a42c9e7d1b30
Revises: e1b7f4a92c33
"""

from alembic import op
import sqlalchemy as sa


revision = "a42c9e7d1b30"
down_revision = "e1b7f4a92c33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("chat_messages") as batch_op:
        batch_op.add_column(sa.Column("cluster_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_chat_messages_cluster_id_clusters", "clusters", ["cluster_id"], ["id"]
        )
    # Existing Chat was default-cluster-only, so all legacy rows belong to
    # the configured default cluster.
    connection = op.get_bind()
    clusters = sa.table(
        "clusters", sa.column("id", sa.String(36)), sa.column("is_default", sa.Boolean())
    )
    chat_messages = sa.table(
        "chat_messages", sa.column("cluster_id", sa.String(36))
    )
    default_cluster_id = connection.execute(
        sa.select(clusters.c.id).where(clusters.c.is_default.is_(True)).limit(1)
    ).scalar_one_or_none()
    if default_cluster_id is not None:
        connection.execute(
            chat_messages.update()
            .where(chat_messages.c.cluster_id.is_(None))
            .values(cluster_id=default_cluster_id)
        )
    op.create_index("ix_chat_messages_actor_cluster_time", "chat_messages", ["actor", "cluster_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_messages_actor_cluster_time", table_name="chat_messages")
    with op.batch_alter_table("chat_messages") as batch_op:
        batch_op.drop_constraint("fk_chat_messages_cluster_id_clusters", type_="foreignkey")
        batch_op.drop_column("cluster_id")
