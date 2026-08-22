import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.osd_latency_monitor as osdlm
from shared import db as db_module
from shared.db import Base
from shared.models import Action, ActionClassification, ActionStatus, AuditEntry, Incident, IncidentStatus
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
    # osdlm._consecutive_high_scans is process-lifetime module state (by
    # design, same as watcher/node_health_monitor.py's own
    # _consecutive_high_scans) — would otherwise leak an osd_id's streak
    # across unrelated test functions.
    osdlm._consecutive_high_scans.clear()
    yield
    osdlm._consecutive_high_scans.clear()


def _osds(up_ids, down_ids=(), hosts=None):
    hosts = hosts or {}
    entries = [{"osd_id": i, "crush_host": hosts.get(i, f"node{i}"), "status": "up"} for i in up_ids]
    entries += [{"osd_id": i, "crush_host": hosts.get(i, f"node{i}"), "status": "down"} for i in down_ids]
    return entries


def _perf(latencies: dict, apply_latencies: dict | None = None):
    # Real shape verified against a live cluster 2026-08-07 — osd_perf_infos
    # is nested under "osdstats", not top-level (see osd_latency_monitor.py's
    # own module docstring).
    apply_latencies = apply_latencies or {}
    return {
        "pg_ready": True,
        "osdstats": {
            "osd_perf_infos": [
                {
                    "id": osd_id,
                    "perf_stats": {
                        "commit_latency_ms": commit_ms,
                        "apply_latency_ms": apply_latencies.get(osd_id, 0.0),
                    },
                }
                for osd_id, commit_ms in latencies.items()
            ]
        },
    }


def _mock_ceph(monkeypatch, latencies, up_ids=None, down_ids=(), hosts=None):
    up_ids = up_ids if up_ids is not None else list(latencies.keys())
    monkeypatch.setattr(
        osdlm.ceph_client, "run_ceph_json_command", lambda cmd: ("mon0", _perf(latencies))
    )
    monkeypatch.setattr(osdlm.ceph_client, "list_osds", lambda: _osds(up_ids, down_ids, hosts))


# --- check_osd_latency_outliers() -------------------------------------------


def test_check_returns_empty_when_query_fails(monkeypatch):
    def boom(cmd):
        raise CephQueryError("no MON nodes reachable")

    monkeypatch.setattr(osdlm.ceph_client, "run_ceph_json_command", boom)

    assert osdlm.check_osd_latency_outliers() == {}


def test_check_returns_empty_when_too_few_up_osds(monkeypatch):
    latencies = {i: 10.0 for i in range(osdlm.MIN_UP_OSDS_FOR_COMPARISON - 1)}
    _mock_ceph(monkeypatch, latencies)

    for _ in range(osdlm.CONSECUTIVE_SCANS_REQUIRED + 1):
        assert osdlm.check_osd_latency_outliers() == {}


def test_check_does_not_flag_when_all_latencies_similar(monkeypatch):
    latencies = {0: 10.0, 1: 11.0, 2: 9.0, 3: 10.5}
    _mock_ceph(monkeypatch, latencies)

    for _ in range(osdlm.CONSECUTIVE_SCANS_REQUIRED + 1):
        assert osdlm.check_osd_latency_outliers() == {}


def test_check_flags_after_consecutive_high_scans(monkeypatch):
    # median of {10,10,10,50} = 10; osd 3 at 50ms is 5x the median, comfortably
    # above both OUTLIER_LATENCY_RATIO (3x) and MIN_ABSOLUTE_LATENCY_MS (5ms).
    latencies = {0: 10.0, 1: 10.0, 2: 10.0, 3: 50.0}
    _mock_ceph(monkeypatch, latencies, hosts={3: "node2"})

    for _ in range(osdlm.CONSECUTIVE_SCANS_REQUIRED - 1):
        assert osdlm.check_osd_latency_outliers() == {}

    result = osdlm.check_osd_latency_outliers()
    assert "OSD_LATENCY_HIGH:3" in result
    detail = result["OSD_LATENCY_HIGH:3"]
    assert detail["osd_id"] == 3
    assert detail["host"] == "node2"
    assert detail["commit_latency_ms"] == 50.0
    assert detail["cluster_median_ms"] == 10.0
    assert detail["consecutive_scans"] == osdlm.CONSECUTIVE_SCANS_REQUIRED


def test_check_resets_streak_when_a_scan_is_normal(monkeypatch):
    high = {0: 10.0, 1: 10.0, 2: 10.0, 3: 50.0}
    normal = {0: 10.0, 1: 10.0, 2: 10.0, 3: 10.0}

    _mock_ceph(monkeypatch, high, hosts={3: "node2"})
    for _ in range(osdlm.CONSECUTIVE_SCANS_REQUIRED - 1):
        osdlm.check_osd_latency_outliers()

    _mock_ceph(monkeypatch, normal, hosts={3: "node2"})
    assert osdlm.check_osd_latency_outliers() == {}

    _mock_ceph(monkeypatch, high, hosts={3: "node2"})
    for _ in range(osdlm.CONSECUTIVE_SCANS_REQUIRED - 1):
        assert osdlm.check_osd_latency_outliers() == {}
    assert "OSD_LATENCY_HIGH:3" in osdlm.check_osd_latency_outliers()


def test_check_ignores_down_osds(monkeypatch):
    # osd 3 reports a huge latency but is DOWN — already covered by
    # `ceph health detail`'s own OSD_DOWN check, must not double-flag here,
    # and must not skew the median used for the still-up OSDs either.
    latencies = {0: 10.0, 1: 10.0, 2: 10.0, 3: 999.0}
    _mock_ceph(monkeypatch, latencies, up_ids=[0, 1, 2], down_ids=[3])

    for _ in range(osdlm.CONSECUTIVE_SCANS_REQUIRED + 1):
        assert osdlm.check_osd_latency_outliers() == {}


def test_check_does_not_false_positive_when_median_is_near_zero(monkeypatch):
    # An all-NVMe cluster where everything is sub-millisecond — one OSD at
    # 0.5ms is "5x" a ~0.1ms median but must not cross MIN_ABSOLUTE_LATENCY_MS.
    latencies = {0: 0.1, 1: 0.1, 2: 0.1, 3: 0.5}
    _mock_ceph(monkeypatch, latencies)

    for _ in range(osdlm.CONSECUTIVE_SCANS_REQUIRED + 1):
        assert osdlm.check_osd_latency_outliers() == {}


# --- create_or_resolve_osd_latency_incidents() ------------------------------


def _detail(osd_id=3, host="node2", commit_ms=50.0, median_ms=10.0, consecutive_scans=2):
    return {
        "osd_id": osd_id,
        "host": host,
        "commit_latency_ms": commit_ms,
        "apply_latency_ms": 0.0,
        "cluster_median_ms": median_ms,
        "consecutive_scans": consecutive_scans,
    }


def test_create_or_resolve_creates_incident_and_investigate_manually_action(isolated_db):
    current = {"OSD_LATENCY_HIGH:3": _detail()}

    osdlm.create_or_resolve_osd_latency_incidents(current)

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="OSD_LATENCY_HIGH:3").one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert "osd.3" in incident.log_excerpt
        assert "node2" in incident.log_excerpt
        evidence = json.loads(incident.signal_evidence_json)
        assert evidence["source"] == "ceph_osd_perf"
        assert evidence["osd_id"] == 3
        assert evidence["commit_latency_ms"] == 50.0

        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert action.action_id == "investigate_manually"
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value

        audit_entry = session.query(AuditEntry).filter_by(incident_id=incident.id).one()
        assert audit_entry.event_type == "risky_action_pending_approval"
        assert audit_entry.actor == "system"


def test_create_or_resolve_does_not_duplicate_an_already_open_incident(isolated_db):
    current = {"OSD_LATENCY_HIGH:3": _detail()}

    osdlm.create_or_resolve_osd_latency_incidents(current)
    osdlm.create_or_resolve_osd_latency_incidents(current)  # same osd still flagged next scan

    with db_module.SessionLocal() as session:
        count = session.query(Incident).filter_by(ceph_code="OSD_LATENCY_HIGH:3").count()
        assert count == 1


def test_create_or_resolve_resolves_when_no_longer_flagged(isolated_db):
    osdlm.create_or_resolve_osd_latency_incidents({"OSD_LATENCY_HIGH:3": _detail()})

    osdlm.create_or_resolve_osd_latency_incidents({})  # latency back to normal, or osd no longer up

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="OSD_LATENCY_HIGH:3").one()
        assert incident.status == IncidentStatus.RESOLVED.value


def test_create_or_resolve_only_touches_its_own_ceph_code_family(isolated_db):
    with db_module.SessionLocal() as session:
        unrelated = Incident(
            ceph_code="OSD_DOWN", status=IncidentStatus.FAILED.value, detected_at=datetime.utcnow()
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.id

    osdlm.create_or_resolve_osd_latency_incidents({})

    with db_module.SessionLocal() as session:
        assert session.get(Incident, unrelated_id).status == IncidentStatus.FAILED.value


def test_create_or_resolve_sends_telegram_alert_only_for_a_newly_created_incident(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        osdlm, "send_osd_latency_alert", lambda osd_id, host, message: calls.append((osd_id, host, message))
    )
    current = {"OSD_LATENCY_HIGH:3": _detail()}

    osdlm.create_or_resolve_osd_latency_incidents(current)
    osdlm.create_or_resolve_osd_latency_incidents(current)  # already open — must NOT alert again

    assert len(calls) == 1
    assert calls[0][0] == 3
    assert calls[0][1] == "node2"


def test_create_or_resolve_does_not_alert_on_resolve(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        osdlm, "send_osd_latency_alert", lambda osd_id, host, message: calls.append((osd_id, host, message))
    )

    osdlm.create_or_resolve_osd_latency_incidents({"OSD_LATENCY_HIGH:3": _detail()})
    calls.clear()
    osdlm.create_or_resolve_osd_latency_incidents({})  # resolve — must not send another alert

    assert calls == []
