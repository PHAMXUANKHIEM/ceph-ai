import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import incident_evidence
from shared.db import Base
from shared.evidence_collectors import OK, EvidenceRunner
from shared.models import Incident, IncidentEvidence, IncidentTimelineEvent
from watcher import investigation_scanner as scanner

NOW = datetime(2026, 9, 28, 12, 0)
MAX_AGE = timedelta(minutes=30)


class FakeTransport:
    mon_nodes = ["10.3.53.1"]

    def __init__(self):
        self.calls = []

    def ceph(self, command, timeout):
        self.calls.append(command)
        return {"cmd": command}

    def host(self, host, command, timeout):
        self.calls.append(f"{host}:{command}")
        return "up 5 days"


def _factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _incident(factory, code, *, age=timedelta(minutes=5), status="PENDING_APPROVAL", evidence=None, cluster="c1"):
    with factory() as session:
        incident = Incident(ceph_code=code, status=status, cluster_id=cluster, created_at=NOW - age,
                            detected_at=NOW - age, signal_evidence_json=json.dumps(evidence) if evidence else None)
        session.add(incident)
        session.commit()
        return incident.id


def _run(factory, transport, runner=None, **kwargs):
    runner = runner or EvidenceRunner(transport)
    return scanner.investigate(factory, transport, runner, cluster_id="c1", now=NOW, max_age=MAX_AGE,
                               limit=kwargs.pop("limit", 2), **kwargs)


def test_new_incident_gets_runbook_evidence_and_a_timeline_event():
    factory = _factory()
    incident_id = _incident(factory, "NODE_UNREACHABLE:10.3.53.9")
    transport = FakeTransport()
    done = _run(factory, transport)
    assert done[0]["incident_id"] == incident_id and done[0]["runbook"] == "NODE_UNREACHABLE"
    assert "10.3.53.1:ping -c 3 -W 1 10.3.53.9" in transport.calls
    assert "10.3.53.9:uptime" in transport.calls
    with factory() as session:
        rows = incident_evidence.for_incident(session, incident_id)
        assert {row.collector_id for row in rows} >= {"mon_ping", "host_uptime", "ceph_osd_tree"}
        assert all(row.status == OK for row in rows)
        event = session.query(IncidentTimelineEvent).filter_by(incident_id=incident_id).one()
        assert event.event_type == incident_evidence.EVENT_COLLECTED
        assert incident_evidence.summary_lines(rows)[0].startswith("Bằng chứng (NODE_UNREACHABLE)")
    # Investigated once: the next tick finds nothing to do.
    assert _run(factory, transport, runner=EvidenceRunner(transport)) == []


def test_old_closed_other_cluster_and_already_investigated_incidents_are_ignored():
    factory = _factory()
    _incident(factory, "OSD_DOWN", age=timedelta(hours=2))
    _incident(factory, "MON_CLOCK_SKEW", status="RESOLVED")
    _incident(factory, "PG_DEGRADED", cluster="other")
    investigated = _incident(factory, "POOL_NEARFULL")
    with factory() as session:
        session.add(IncidentEvidence(incident_id=investigated, runbook="x", collector_id="x", target="-", status=OK))
        session.commit()
    transport = FakeTransport()
    assert _run(factory, transport) == []
    assert transport.calls == []


def test_unscoped_legacy_rows_are_included_only_when_asked():
    factory = _factory()
    _incident(factory, "POOL_NEARFULL", cluster=None)
    assert _run(factory, FakeTransport()) == []
    assert len(_run(factory, FakeTransport(), include_unscoped=True)) == 1


def test_flapping_repeat_is_marked_without_ssh():
    factory = _factory()
    incident_id = _incident(factory, "NODE_UNREACHABLE:10.3.53.9", evidence={"flapping": True})
    transport = FakeTransport()
    assert _run(factory, transport) == []
    assert transport.calls == []
    with factory() as session:
        (row,) = incident_evidence.for_incident(session, incident_id)
        assert row.status == incident_evidence.SKIPPED_FLAPPING


def test_a_burst_of_one_family_costs_one_run_and_limit_is_respected():
    factory = _factory()
    for index in range(3):
        _incident(factory, f"OSD_LATENCY_HIGH:{index}", age=timedelta(minutes=10 - index))
    _incident(factory, "MON_CLOCK_SKEW", age=timedelta(minutes=1))
    _incident(factory, "PG_DEGRADED", age=timedelta(seconds=30))
    done = _run(factory, FakeTransport(), limit=2)
    assert [item["runbook"] for item in done] == ["OSD_LATENCY_HIGH", "MON_CLOCK_SKEW"]


def test_missing_context_is_stored_as_skipped():
    factory = _factory()
    incident_id = _incident(factory, "OSD_LATENCY_HIGH")
    _run(factory, FakeTransport())
    with factory() as session:
        skipped = {row.collector_id for row in incident_evidence.for_incident(session, incident_id)
                   if row.status == incident_evidence.SKIPPED_CONTEXT}
    assert skipped == {"ceph_osd_slow_ops", "ceph_osd_metadata"}


def test_scan_is_off_when_disabled_or_without_mon_nodes(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "investigation_enabled", False)
    assert scanner.scan_default_cluster("c1") == []
    monkeypatch.setattr(settings, "investigation_enabled", True)
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    assert scanner.scan_default_cluster("c1") == []


def test_timeline_page_shows_collected_evidence(dashboard_client):
    from shared import db as db_module

    with db_module.SessionLocal() as session:
        session.add(Incident(id="ev-inc", ceph_code="OSD_LATENCY_HIGH:3", status="PENDING_APPROVAL",
                             detected_at=datetime.utcnow()))
        session.flush()
        session.add(IncidentEvidence(incident_id="ev-inc", runbook="OSD_LATENCY_HIGH", collector_id="ceph_osd_perf",
                                     target="mon", status=OK, command="ceph osd perf",
                                     output_redacted='{"osd": 3, "commit_latency_ms": 180}', duration_ms=4200))
        session.add(IncidentEvidence(incident_id="ev-inc", runbook="OSD_LATENCY_HIGH", collector_id="ceph_osd_df",
                                     target="mon", status="timeout", command="ceph osd df", duration_ms=20000))
        session.commit()
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    page = dashboard_client.get("/incidents/ev-inc/timeline")
    assert page.status_code == 200
    assert "Bằng chứng tự động" in page.text
    assert "Bằng chứng (OSD_LATENCY_HIGH): 1/2 collector thành công" in page.text
    assert "ceph_osd_df=timeout" in page.text
    assert "commit_latency_ms" in page.text
