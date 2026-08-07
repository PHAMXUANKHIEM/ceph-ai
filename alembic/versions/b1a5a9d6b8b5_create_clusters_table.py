"""create clusters table

Multi-cluster observability Phase 1 — see shared/models.py::Cluster's
docstring. Deliberately spliced off the last COMMITTED migration
(6b1f3a9d7e2c) rather than chaining after the uncommitted CRUSH-monitor
migrations (85650f5c02f3/be5e3bfbfac1) sitting in the working tree at the
time this was written — those are separate, unrelated in-progress work.
This creates a second head; reconcile with a merge migration once both are
committed and their real order is decided.

Revision ID: b1a5a9d6b8b5
Revises: 6b1f3a9d7e2c
Create Date: 2026-08-07 16:10:26.655122

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1a5a9d6b8b5'
down_revision: Union[str, Sequence[str], None] = '6b1f3a9d7e2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'clusters',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('ceph_mon_nodes', sa.Text(), nullable=False),
        sa.Column('ceph_container_name', sa.String(length=128), nullable=False),
        sa.Column('ssh_user', sa.String(length=64), nullable=False),
        sa.Column('ssh_key_path', sa.Text(), nullable=False),
        sa.Column('ceph_exec_mode', sa.String(length=16), nullable=False),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    # Partial unique index, not just application-level idempotency in
    # shared/clusters.py::ensure_default_cluster — Watcher/Worker/Dashboard
    # all call that function at startup and can race each other on a fresh
    # deploy; without a real DB constraint here, two concurrent inserts
    # (both querying "no default exists yet" before either commits) would
    # both succeed, leaving two is_default=True rows.
    op.create_index(
        'uq_clusters_single_default',
        'clusters',
        ['is_default'],
        unique=True,
        sqlite_where=sa.text('is_default'),
        postgresql_where=sa.text('is_default'),
    )

    op.add_column('incidents', sa.Column('cluster_id', sa.String(length=36), nullable=True))
    with op.batch_alter_table('incidents') as batch_op:
        batch_op.create_foreign_key('fk_incidents_cluster_id', 'clusters', ['cluster_id'], ['id'])

    op.add_column('watcher_heartbeat', sa.Column('cluster_id', sa.String(length=36), nullable=True))
    with op.batch_alter_table('watcher_heartbeat') as batch_op:
        batch_op.create_foreign_key('fk_watcher_heartbeat_cluster_id', 'clusters', ['cluster_id'], ['id'])
        batch_op.create_unique_constraint('uq_watcher_heartbeat_cluster_id', ['cluster_id'])


def downgrade() -> None:
    with op.batch_alter_table('watcher_heartbeat') as batch_op:
        batch_op.drop_constraint('uq_watcher_heartbeat_cluster_id', type_='unique')
        batch_op.drop_constraint('fk_watcher_heartbeat_cluster_id', type_='foreignkey')
    op.drop_column('watcher_heartbeat', 'cluster_id')

    with op.batch_alter_table('incidents') as batch_op:
        batch_op.drop_constraint('fk_incidents_cluster_id', type_='foreignkey')
    op.drop_column('incidents', 'cluster_id')

    op.drop_index('uq_clusters_single_default', table_name='clusters')
    op.drop_table('clusters')
