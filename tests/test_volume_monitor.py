from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.volume_monitor as vm
from shared import db as db_module
from shared.db import Base
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    Incident,
    IncidentStatus,
    VolumeMetric,
)
from watcher.ceph_client import CephQueryError


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    yield engine


@pytest.fixture(autouse=True)
def clear_module_state():
    # vm._state/_last_poll_samples are process-lifetime module state (by
    # design — see volume_monitor.py's own docstring), which would
    # otherwise leak a volume's rolling window / last-poll samples across
    # unrelated test functions (including into other test files, since
    # this is a plain module-level variable, not per-test).
    vm._state.clear()
    vm._last_poll_samples.clear()
    yield
    vm._state.clear()
    vm._last_poll_samples.clear()


def _sample(pool="vms", image="disk-1", iops=100.0, read_latency_ms=1.0, write_latency_ms=1.0):
    return {
        "pool": pool,
        "image": image,
        "iops": iops,
        "read_latency_ms": read_latency_ms,
        "write_latency_ms": write_latency_ms,
    }


# --- check_volumes() ---------------------------------------------------


def test_check_volumes_returns_empty_when_no_pools_configured(monkeypatch):
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: [])

    assert vm.check_volumes() == {}


def test_check_volumes_needs_a_full_window_before_ever_flagging(monkeypatch):
    # Feed ROLLING_WINDOW_SIZE - 1 samples that WOULD otherwise look
    # saturated (near-peak IOPS + elevated latency) — must never flag while
    # the window hasn't filled yet, since there's no baseline to judge
    # "elevated" against.
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: ["vms"])

    calls = {"n": 0}

    def fake_iostat(pool):
        calls["n"] += 1
        return [_sample(iops=100.0, read_latency_ms=1.0, write_latency_ms=1.0)]

    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", fake_iostat)

    for _ in range(vm.ROLLING_WINDOW_SIZE - 1):
        assert vm.check_volumes() == {}
    assert calls["n"] == vm.ROLLING_WINDOW_SIZE - 1


def test_check_volumes_flags_after_consecutive_saturated_polls(monkeypatch):
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: ["vms"])

    # Warm up the window with steady, low-latency samples (establishes a
    # baseline + a peak IOPS to compare a later spike against).
    steady = [_sample(iops=100.0, read_latency_ms=1.0, write_latency_ms=1.0)] * vm.ROLLING_WINDOW_SIZE
    it = iter(steady)
    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [next(it)])
    for _ in range(vm.ROLLING_WINDOW_SIZE):
        assert vm.check_volumes() == {}

    # Now feed samples that ARE near-peak IOPS with clearly elevated latency
    # — must take CONSECUTIVE_POLLS_REQUIRED in a row before an Incident
    # candidate appears, never on the first one.
    saturated_sample = _sample(iops=95.0, read_latency_ms=10.0, write_latency_ms=10.0)
    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [saturated_sample])

    for _ in range(vm.CONSECUTIVE_POLLS_REQUIRED - 1):
        assert vm.check_volumes() == {}

    result = vm.check_volumes()
    assert "VOLUME_SATURATED:vms/disk-1" in result
    detail = result["VOLUME_SATURATED:vms/disk-1"]
    assert detail["pool"] == "vms"
    assert detail["image"] == "disk-1"
    assert detail["consecutive_polls"] == vm.CONSECUTIVE_POLLS_REQUIRED


def test_check_volumes_does_not_flag_high_iops_with_normal_latency(monkeypatch):
    # High IOPS alone is legitimate heavy load, not saturation — must not
    # false-positive without the accompanying latency spike.
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: ["vms"])
    busy_but_healthy = _sample(iops=100.0, read_latency_ms=1.0, write_latency_ms=1.0)
    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [busy_but_healthy])

    for _ in range(vm.ROLLING_WINDOW_SIZE + vm.CONSECUTIVE_POLLS_REQUIRED):
        assert vm.check_volumes() == {}


def test_check_volumes_resets_streak_when_a_sample_is_not_saturated(monkeypatch):
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: ["vms"])
    steady = [_sample(iops=100.0, read_latency_ms=1.0, write_latency_ms=1.0)] * vm.ROLLING_WINDOW_SIZE
    it = iter(steady)
    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [next(it)])
    for _ in range(vm.ROLLING_WINDOW_SIZE):
        vm.check_volumes()

    saturated = _sample(iops=95.0, read_latency_ms=10.0, write_latency_ms=10.0)
    healthy = _sample(iops=95.0, read_latency_ms=1.0, write_latency_ms=1.0)

    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [saturated])
    for _ in range(vm.CONSECUTIVE_POLLS_REQUIRED - 1):
        vm.check_volumes()

    # One healthy sample breaks the streak — the next saturated sample must
    # start counting from zero again, not carry the prior streak forward.
    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [healthy])
    assert vm.check_volumes() == {}

    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [saturated])
    for _ in range(vm.CONSECUTIVE_POLLS_REQUIRED - 1):
        assert vm.check_volumes() == {}
    assert "VOLUME_SATURATED:vms/disk-1" in vm.check_volumes()


def test_check_volumes_skips_a_pool_that_fails_to_query(monkeypatch):
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: ["broken", "vms"])

    def fake_iostat(pool):
        if pool == "broken":
            raise CephQueryError("all MON nodes failed")
        return []

    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", fake_iostat)

    assert vm.check_volumes() == {}  # must not raise


# --- create_or_resolve_volume_incidents() -------------------------------


def _detail(pool="vms", image="disk-1", iops=95.0, latency_ms=10.0, consecutive_polls=3):
    return {
        "pool": pool,
        "image": image,
        "iops": iops,
        "latency_ms": latency_ms,
        "consecutive_polls": consecutive_polls,
    }


def test_create_or_resolve_creates_incident_and_investigate_manually_action(isolated_db):
    current = {"VOLUME_SATURATED:vms/disk-1": _detail()}

    vm.create_or_resolve_volume_incidents(current)

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="VOLUME_SATURATED:vms/disk-1").one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert "disk-1" in incident.log_excerpt

        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert action.action_id == "investigate_manually"
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value

        audit_entry = session.query(AuditEntry).filter_by(incident_id=incident.id).one()
        assert audit_entry.event_type == "risky_action_pending_approval"
        assert audit_entry.actor == "system"


def test_create_or_resolve_does_not_duplicate_an_already_open_incident(isolated_db):
    current = {"VOLUME_SATURATED:vms/disk-1": _detail()}

    vm.create_or_resolve_volume_incidents(current)
    vm.create_or_resolve_volume_incidents(current)  # same volume still saturated next poll

    with db_module.SessionLocal() as session:
        count = session.query(Incident).filter_by(ceph_code="VOLUME_SATURATED:vms/disk-1").count()
        assert count == 1


def test_create_or_resolve_resolves_when_volume_no_longer_saturated(isolated_db):
    vm.create_or_resolve_volume_incidents({"VOLUME_SATURATED:vms/disk-1": _detail()})

    vm.create_or_resolve_volume_incidents({})  # no longer saturated

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="VOLUME_SATURATED:vms/disk-1").one()
        assert incident.status == IncidentStatus.RESOLVED.value


def test_create_or_resolve_only_touches_its_own_ceph_code_family(isolated_db):
    # An unrelated open Incident (real Ceph check) must never be resolved
    # just because it's not in current_saturated — this function only
    # queries/touches VOLUME_SATURATED: rows.
    with db_module.SessionLocal() as session:
        unrelated = Incident(
            ceph_code="OSD_DOWN", status=IncidentStatus.FAILED.value, detected_at=datetime.utcnow()
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.id

    vm.create_or_resolve_volume_incidents({})

    with db_module.SessionLocal() as session:
        assert session.get(Incident, unrelated_id).status == IncidentStatus.FAILED.value


# --- persist_last_poll_metrics() ---------------------------------------


def test_persist_last_poll_metrics_writes_a_row_per_sample_from_the_last_check_volumes_call(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: ["vms"])
    monkeypatch.setattr(
        vm.ceph_client,
        "query_rbd_iostat",
        lambda pool: [
            _sample(image="disk-1", iops=100.0, read_latency_ms=1.0, write_latency_ms=1.5),
            _sample(image="disk-2", iops=50.0, read_latency_ms=0.5, write_latency_ms=0.5),
        ],
    )

    vm.check_volumes()
    vm.persist_last_poll_metrics()

    with db_module.SessionLocal() as session:
        rows = session.query(VolumeMetric).order_by(VolumeMetric.image).all()
        assert [r.image for r in rows] == ["disk-1", "disk-2"]
        assert rows[0].pool == "vms"
        assert rows[0].iops == 100.0
        assert rows[0].read_latency_ms == 1.0
        assert rows[0].write_latency_ms == 1.5
        assert rows[0].saturated is False  # window not full yet on the first poll


def test_persist_last_poll_metrics_marks_saturated_flag_once_streak_is_reached(isolated_db, monkeypatch):
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: ["vms"])
    steady = [_sample(iops=100.0, read_latency_ms=1.0, write_latency_ms=1.0)] * vm.ROLLING_WINDOW_SIZE
    it = iter(steady)
    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [next(it)])
    for _ in range(vm.ROLLING_WINDOW_SIZE):
        vm.check_volumes()

    saturated_sample = _sample(iops=95.0, read_latency_ms=10.0, write_latency_ms=10.0)
    monkeypatch.setattr(vm.ceph_client, "query_rbd_iostat", lambda pool: [saturated_sample])
    for _ in range(vm.CONSECUTIVE_POLLS_REQUIRED):
        vm.check_volumes()
    vm.persist_last_poll_metrics()

    with db_module.SessionLocal() as session:
        # Only the FINAL poll's samples are persisted here (each
        # check_volumes() call overwrites _last_poll_samples) — this test
        # only calls persist_last_poll_metrics() once, after the streak
        # already reached CONSECUTIVE_POLLS_REQUIRED.
        row = session.query(VolumeMetric).one()
        assert row.saturated is True


def test_persist_last_poll_metrics_is_a_noop_when_nothing_was_polled(isolated_db, monkeypatch):
    monkeypatch.setattr(vm.ceph_client, "configured_rbd_pools", lambda: [])

    vm.check_volumes()
    vm.persist_last_poll_metrics()

    with db_module.SessionLocal() as session:
        assert session.query(VolumeMetric).count() == 0
