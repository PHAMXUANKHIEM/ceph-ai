import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.models import Cluster, Incident, IncidentStatus, LogFinding, LogIngestRun
from watcher.incident_correlation import correlate_finding, family_matches_code


def _session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(session, *, incident_code="OSD_DOWN", incident_excerpt="osd.5 down", finding_osd="5"):
    now = datetime(2026, 8, 22, 9, 0)
    cluster = Cluster(
        name="default", ceph_mon_nodes="10.0.0.1", ssh_user="root",
        ssh_key_path="/key", is_default=True,
    )
    session.add(cluster)
    session.flush()
    run = LogIngestRun(
        cluster_id=cluster.id, source="loki", window_start=now - timedelta(hours=1),
        window_end=now, status="OK",
    )
    incident = Incident(
        cluster_id=cluster.id, ceph_code=incident_code, status=IncidentStatus.NEW.value,
        log_excerpt=incident_excerpt, detected_at=now - timedelta(minutes=10),
        signal_evidence_json=json.dumps({"source": "ceph_osd_perf", "osd_id": 5, "commit_latency_ms": 42}),
    )
    session.add_all([run, incident])
    session.flush()
    finding = LogFinding(
        cluster_id=cluster.id, ingest_run_id=run.id, verdict="FINDING",
        dedupe_key="dedupe", fault_family="network_heartbeat",
        semantic_entities_json=json.dumps([f"daemon:osd.{finding_osd}"]),
    )
    session.add(finding)
    session.flush()
    return now, finding, incident


def test_family_catalogue_rejects_log_intel_synthetic_incident():
    assert family_matches_code("network_heartbeat", "OSD_DOWN")
    assert not family_matches_code("network_heartbeat", "LOG_INTEL_abc")


def test_capacity_family_accepts_real_ceph_code_variants():
    assert family_matches_code("capacity_pressure", "OSD_BACKFILLFULL")
    assert family_matches_code("capacity_pressure", "POOL_NEAR_FULL")


def test_correlates_same_family_and_osd():
    session = _session()
    now, finding, incident = _seed(session)
    assert correlate_finding(session, finding, now=now) is incident
    assert finding.correlated_incident_id == incident.id
    assert "osd=5" in finding.correlation_reason
    assert json.loads(finding.correlation_evidence_json)["commit_latency_ms"] == 42


def test_does_not_correlate_conflicting_osd_entity():
    session = _session()
    now, finding, _ = _seed(session, finding_osd="7")
    assert correlate_finding(session, finding, now=now) is None
    assert finding.correlated_incident_id is None


def test_does_not_correlate_wrong_fault_family():
    session = _session()
    now, finding, _ = _seed(session, incident_code="MON_CLOCK_SKEW", incident_excerpt="clock skew")
    assert correlate_finding(session, finding, now=now) is None


def test_does_not_correlate_resolved_incident():
    session = _session()
    now, finding, incident = _seed(session)
    incident.status = IncidentStatus.RESOLVED.value
    assert correlate_finding(session, finding, now=now) is None
