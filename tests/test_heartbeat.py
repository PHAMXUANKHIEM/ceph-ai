from datetime import datetime

from shared import heartbeat
from shared.models import Cluster, WatcherHeartbeat


def _make_cluster(session, cluster_id: str, *, is_default: bool = False) -> str:
    """WatcherHeartbeat.cluster_id is a real FK to clusters.id — the
    db_session fixture enforces foreign keys (shared/db.py::make_engine),
    so every cluster_id these tests record a heartbeat for must be a real
    row, not an arbitrary string."""
    session.add(
        Cluster(
            id=cluster_id,
            name=cluster_id,
            ceph_mon_nodes="10.20.1.150",
            ceph_container_name="",
            ssh_user="root",
            ssh_key_path="/root/.ssh/key",
            ceph_exec_mode="docker",
            is_default=is_default,
            is_active=True,
        )
    )
    session.commit()
    return cluster_id


def test_record_creates_row_when_none_exists(db_session):
    cluster_id = _make_cluster(db_session, "cluster-a")
    polled_at = datetime.utcnow()

    heartbeat.record(
        db_session,
        cluster_id=cluster_id,
        success=True,
        mon_node="10.20.1.150",
        error_message=None,
        polled_at=polled_at,
    )
    db_session.commit()

    row = db_session.query(WatcherHeartbeat).one()
    assert row.cluster_id == cluster_id
    assert row.success is True
    assert row.mon_node == "10.20.1.150"
    assert row.error_message is None
    assert row.polled_at == polled_at


def test_record_updates_existing_row_in_place_not_a_new_one(db_session):
    cluster_id = _make_cluster(db_session, "cluster-a")
    heartbeat.record(
        db_session,
        cluster_id=cluster_id,
        success=True,
        mon_node="10.20.1.150",
        error_message=None,
        polled_at=datetime.utcnow(),
    )
    db_session.commit()

    second_polled_at = datetime.utcnow()
    heartbeat.record(
        db_session,
        cluster_id=cluster_id,
        success=False,
        mon_node=None,
        error_message="All MON nodes failed",
        polled_at=second_polled_at,
    )
    db_session.commit()

    assert db_session.query(WatcherHeartbeat).count() == 1
    row = db_session.query(WatcherHeartbeat).one()
    assert row.success is False
    assert row.mon_node is None
    assert row.error_message == "All MON nodes failed"
    assert row.polled_at == second_polled_at


def test_record_keys_by_cluster_id_not_a_single_singleton_row(db_session):
    """Multi-cluster observability Phase 1: each cluster_id gets its own
    row now, unlike the old fixed id=1 singleton."""
    cluster_a = _make_cluster(db_session, "cluster-a", is_default=True)
    cluster_b = _make_cluster(db_session, "cluster-b")

    heartbeat.record(
        db_session,
        cluster_id=cluster_a,
        success=True,
        mon_node="10.20.1.150",
        error_message=None,
        polled_at=datetime.utcnow(),
    )
    heartbeat.record(
        db_session,
        cluster_id=cluster_b,
        success=False,
        mon_node=None,
        error_message="unreachable",
        polled_at=datetime.utcnow(),
    )
    db_session.commit()

    assert db_session.query(WatcherHeartbeat).count() == 2
    row_a = heartbeat.get_latest(db_session, cluster_a)
    row_b = heartbeat.get_latest(db_session, cluster_b)
    assert row_a.success is True
    assert row_b.success is False
    assert row_b.error_message == "unreachable"


def test_record_does_not_commit_itself(db_session):
    cluster_id = _make_cluster(db_session, "cluster-a")
    heartbeat.record(
        db_session,
        cluster_id=cluster_id,
        success=True,
        mon_node="10.20.1.150",
        error_message=None,
        polled_at=datetime.utcnow(),
    )
    db_session.rollback()

    assert db_session.query(WatcherHeartbeat).count() == 0


def test_get_latest_returns_none_when_never_recorded(db_session):
    cluster_id = _make_cluster(db_session, "cluster-a")
    assert heartbeat.get_latest(db_session, cluster_id) is None


def test_get_latest_returns_the_recorded_row(db_session):
    cluster_id = _make_cluster(db_session, "cluster-a")
    polled_at = datetime.utcnow()
    heartbeat.record(
        db_session,
        cluster_id=cluster_id,
        success=True,
        mon_node="10.20.1.150",
        error_message=None,
        polled_at=polled_at,
    )
    db_session.commit()

    result = heartbeat.get_latest(db_session, cluster_id)

    assert result is not None
    assert result.mon_node == "10.20.1.150"
    assert result.polled_at == polled_at


def test_get_latest_returns_none_for_a_different_cluster(db_session):
    cluster_a = _make_cluster(db_session, "cluster-a")
    cluster_b = _make_cluster(db_session, "cluster-b")
    heartbeat.record(
        db_session,
        cluster_id=cluster_a,
        success=True,
        mon_node="10.20.1.150",
        error_message=None,
        polled_at=datetime.utcnow(),
    )
    db_session.commit()

    assert heartbeat.get_latest(db_session, cluster_b) is None
