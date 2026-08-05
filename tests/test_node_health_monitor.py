from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.node_health_monitor as nhm
from shared import db as db_module
from shared.db import Base
from shared.models import Action, ActionClassification, ActionStatus, AuditEntry, Incident, IncidentStatus
from watcher.node_metrics import NodeMetricsError


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
    # nhm._consecutive_high_scans is process-lifetime module state (by
    # design, same as watcher/volume_monitor.py's own _state) — would
    # otherwise leak a host's streak across unrelated test functions.
    nhm._consecutive_high_scans.clear()
    yield
    nhm._consecutive_high_scans.clear()


def _metrics(cpu=10.0, mem=10.0):
    return {"cpu_percent": cpu, "mem_percent": mem, "mem_used_mb": 0, "mem_total_mb": 0,
            "disk_read_iops": 0, "disk_write_iops": 0, "disk_latency_ms": 0}


# --- check_node_resources() -------------------------------------------------


def test_check_node_resources_returns_empty_when_no_nodes_configured(monkeypatch):
    monkeypatch.setattr(nhm, "configured_nodes", lambda: [])

    assert nhm.check_node_resources() == {}


def test_check_node_resources_does_not_flag_below_threshold(monkeypatch):
    monkeypatch.setattr(nhm, "configured_nodes", lambda: [{"host": "10.0.0.5", "roles": ["MON"]}])
    monkeypatch.setattr(nhm, "collect_node_metrics", lambda host: _metrics(cpu=50.0, mem=50.0))

    for _ in range(nhm.CONSECUTIVE_SCANS_REQUIRED + 1):
        assert nhm.check_node_resources() == {}


def test_check_node_resources_flags_after_consecutive_high_scans(monkeypatch):
    monkeypatch.setattr(nhm, "configured_nodes", lambda: [{"host": "10.0.0.5", "roles": ["MON"]}])
    monkeypatch.setattr(nhm, "collect_node_metrics", lambda host: _metrics(cpu=95.0, mem=40.0))

    for _ in range(nhm.CONSECUTIVE_SCANS_REQUIRED - 1):
        assert nhm.check_node_resources() == {}

    result = nhm.check_node_resources()
    assert "NODE_RESOURCE_HIGH:10.0.0.5" in result
    detail = result["NODE_RESOURCE_HIGH:10.0.0.5"]
    assert detail["host"] == "10.0.0.5"
    assert detail["cpu_percent"] == 95.0
    assert detail["consecutive_scans"] == nhm.CONSECUTIVE_SCANS_REQUIRED


def test_check_node_resources_flags_on_ram_alone(monkeypatch):
    monkeypatch.setattr(nhm, "configured_nodes", lambda: [{"host": "10.0.0.5", "roles": ["OSD"]}])
    monkeypatch.setattr(nhm, "collect_node_metrics", lambda host: _metrics(cpu=20.0, mem=95.0))

    for _ in range(nhm.CONSECUTIVE_SCANS_REQUIRED):
        result = nhm.check_node_resources()

    assert "NODE_RESOURCE_HIGH:10.0.0.5" in result


def test_check_node_resources_resets_streak_when_a_scan_is_normal(monkeypatch):
    monkeypatch.setattr(nhm, "configured_nodes", lambda: [{"host": "10.0.0.5", "roles": ["MON"]}])

    high = _metrics(cpu=95.0, mem=40.0)
    normal = _metrics(cpu=20.0, mem=20.0)

    monkeypatch.setattr(nhm, "collect_node_metrics", lambda host: high)
    for _ in range(nhm.CONSECUTIVE_SCANS_REQUIRED - 1):
        nhm.check_node_resources()

    monkeypatch.setattr(nhm, "collect_node_metrics", lambda host: normal)
    assert nhm.check_node_resources() == {}

    monkeypatch.setattr(nhm, "collect_node_metrics", lambda host: high)
    for _ in range(nhm.CONSECUTIVE_SCANS_REQUIRED - 1):
        assert nhm.check_node_resources() == {}
    assert "NODE_RESOURCE_HIGH:10.0.0.5" in nhm.check_node_resources()


def test_check_node_resources_skips_a_host_that_fails_to_collect(monkeypatch):
    monkeypatch.setattr(
        nhm, "configured_nodes", lambda: [{"host": "broken", "roles": ["MON"]}, {"host": "ok", "roles": ["OSD"]}]
    )

    def fake_collect(host):
        if host == "broken":
            raise NodeMetricsError("ssh failed")
        return _metrics(cpu=10.0, mem=10.0)

    monkeypatch.setattr(nhm, "collect_node_metrics", fake_collect)

    assert nhm.check_node_resources() == {}  # must not raise


# --- create_or_resolve_node_health_incidents() ------------------------------


def _detail(host="10.0.0.5", cpu=95.0, mem=40.0, consecutive_scans=2):
    return {"host": host, "cpu_percent": cpu, "mem_percent": mem, "consecutive_scans": consecutive_scans}


def test_create_or_resolve_creates_incident_and_investigate_manually_action(isolated_db):
    current = {"NODE_RESOURCE_HIGH:10.0.0.5": _detail()}

    nhm.create_or_resolve_node_health_incidents(current)

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="NODE_RESOURCE_HIGH:10.0.0.5").one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert "10.0.0.5" in incident.log_excerpt

        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert action.action_id == "investigate_manually"
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value

        audit_entry = session.query(AuditEntry).filter_by(incident_id=incident.id).one()
        assert audit_entry.event_type == "risky_action_pending_approval"
        assert audit_entry.actor == "system"


def test_create_or_resolve_does_not_duplicate_an_already_open_incident(isolated_db):
    current = {"NODE_RESOURCE_HIGH:10.0.0.5": _detail()}

    nhm.create_or_resolve_node_health_incidents(current)
    nhm.create_or_resolve_node_health_incidents(current)  # same host still flagged next scan

    with db_module.SessionLocal() as session:
        count = session.query(Incident).filter_by(ceph_code="NODE_RESOURCE_HIGH:10.0.0.5").count()
        assert count == 1


def test_create_or_resolve_resolves_when_no_longer_flagged(isolated_db):
    nhm.create_or_resolve_node_health_incidents({"NODE_RESOURCE_HIGH:10.0.0.5": _detail()})

    nhm.create_or_resolve_node_health_incidents({})  # no longer over threshold

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="NODE_RESOURCE_HIGH:10.0.0.5").one()
        assert incident.status == IncidentStatus.RESOLVED.value


def test_create_or_resolve_only_touches_its_own_ceph_code_family(isolated_db):
    with db_module.SessionLocal() as session:
        unrelated = Incident(
            ceph_code="OSD_DOWN", status=IncidentStatus.FAILED.value, detected_at=datetime.utcnow()
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.id

    nhm.create_or_resolve_node_health_incidents({})

    with db_module.SessionLocal() as session:
        assert session.get(Incident, unrelated_id).status == IncidentStatus.FAILED.value


def test_create_or_resolve_sends_telegram_alert_only_for_a_newly_created_incident(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(nhm, "send_node_alert", lambda host, message: calls.append((host, message)))
    current = {"NODE_RESOURCE_HIGH:10.0.0.5": _detail()}

    nhm.create_or_resolve_node_health_incidents(current)
    nhm.create_or_resolve_node_health_incidents(current)  # already open — must NOT alert again

    assert len(calls) == 1
    assert calls[0][0] == "10.0.0.5"


def test_create_or_resolve_does_not_alert_on_resolve(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(nhm, "send_node_alert", lambda host, message: calls.append((host, message)))

    nhm.create_or_resolve_node_health_incidents({"NODE_RESOURCE_HIGH:10.0.0.5": _detail()})
    calls.clear()
    nhm.create_or_resolve_node_health_incidents({})  # resolve — must not send another alert

    assert calls == []
