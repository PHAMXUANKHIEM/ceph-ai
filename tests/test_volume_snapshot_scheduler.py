import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db
from shared.db import Base
from shared.models import Action, ActionClassification, ActionStatus, Cluster, VolumeSnapshotPolicy
from worker import volume_snapshot_scheduler as snapshot_scheduler


@pytest.fixture()
def isolated_db(monkeypatch):
    test_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(test_engine, "connect", lambda dbapi_conn, _: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(db, "engine", test_engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=test_engine, autoflush=False, autocommit=False))
    yield test_engine


def _cluster():
    cluster = Cluster(
        name="snapshot-cluster", ceph_mon_nodes="10.20.1.10",
        ssh_user="root", ssh_key_path="/root/.ssh/id_rsa", is_default=True,
        is_active=True, openstack_controller_nodes="10.20.1.20",
        openstack_openrc_path="/root/admin-openrc",
    )
    with db.SessionLocal() as session:
        session.add(cluster)
        session.commit()
        return cluster.id


def _policy(cluster_id, **overrides):
    values = dict(
        cluster_id=cluster_id, pool="vms", image="volume-12345678-1234-4123-8123-1234567890ab",
        volume_id="12345678-1234-4123-8123-1234567890ab", snapshot_prefix="scheduled",
        cron_expression="0 2 * * *", timezone="UTC", retention_count=1,
        capacity_guard_percent=85, enabled=True, created_by="admin",
    )
    values.update(overrides)
    with db.SessionLocal() as session:
        policy = VolumeSnapshotPolicy(**values)
        session.add(policy)
        session.commit()
        return policy.id


def test_run_policy_creates_snapshot_and_approval_gated_retention_action(isolated_db, monkeypatch):
    cluster_id = _cluster()
    policy_id = _policy(cluster_id)
    monkeypatch.setattr(snapshot_scheduler.ceph_client, "query_rbd_pool_overview", lambda pool: {"percent_used": 40})
    monkeypatch.setattr(snapshot_scheduler, "discover_cinder_volume", lambda cluster, image: {
        "status": "managed", "verified": True, "volume_id": "12345678-1234-4123-8123-1234567890ab",
        "volume_status": "available",
    })
    monkeypatch.setattr(snapshot_scheduler, "discover_cinder_snapshots", lambda cluster, volume_id: {
        "status": "ok", "items": [
            {"snapshot_id": "abcdefab-1234-4123-8123-1234567890ab", "status": "available", "created_at": "2020-01-01T00:00:00Z"},
            {"snapshot_id": "fedcbafe-1234-4123-8123-1234567890ab", "status": "available", "created_at": "2026-01-01T00:00:00Z"},
        ],
    })
    monkeypatch.setattr(snapshot_scheduler.commands, "get_command", lambda *args: "openstack snapshot command")

    snapshot_scheduler.run_policy(policy_id)

    with db.SessionLocal() as session:
        actions = session.query(Action).order_by(Action.created_at.asc()).all()
        assert [item.action_id for item in actions] == ["cinder_create_snapshot", "cinder_delete_snapshot"]
        assert actions[0].status == ActionStatus.APPROVED.value
        assert actions[0].classification == ActionClassification.RISKY.value
        assert actions[1].status == ActionStatus.PENDING_APPROVAL.value
        assert actions[1].classification == ActionClassification.DESTRUCTIVE.value
        assert json.loads(actions[1].action_params)["snapshot_id"] == "abcdefab-1234-4123-8123-1234567890ab"
        policy = session.get(VolumeSnapshotPolicy, policy_id)
        assert policy.last_status == "scheduled"


def test_run_policy_stops_at_capacity_guard_before_cinder(monkeypatch, isolated_db):
    cluster_id = _cluster()
    policy_id = _policy(cluster_id, capacity_guard_percent=80)
    monkeypatch.setattr(snapshot_scheduler.ceph_client, "query_rbd_pool_overview", lambda pool: {"percent_used": 90})
    called = []
    monkeypatch.setattr(snapshot_scheduler, "discover_cinder_volume", lambda *args: called.append(args))

    snapshot_scheduler.run_policy(policy_id)

    assert called == []
    with db.SessionLocal() as session:
        assert session.query(Action).count() == 0
        policy = session.get(VolumeSnapshotPolicy, policy_id)
        assert policy.last_status == "capacity_guard"
