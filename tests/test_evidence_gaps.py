"""FL6.1: rank the fault families that keep lacking evidence."""

import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.evidence_gaps import evidence_gap_queue, family_of_code
from shared.models import Action, Cluster, Incident, IncidentTimelineEvent, LogFinding, LogIngestRun, RemediationCase
from watcher.incident_correlation import FAMILY_CODES

NOW = datetime(2026, 10, 9, 8, 0)
FAULTS = {"osd_down_fault": {"OSD_DOWN", "PG_DEGRADED", "OSD_HOST_DOWN"},
          "osd_nearfull_fault": {"OSD_NEARFULL", "POOL_NEARFULL"}}


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _ingest_run(session):
    if session.get(Cluster, "c1") is None:
        session.add(Cluster(id="c1", name="CS-LAB", ceph_mon_nodes="", ssh_user="root", ssh_key_path="/tmp/key"))
        session.add(LogIngestRun(id="r1", cluster_id="c1", source="loki", window_start=NOW, window_end=NOW,
                                 status="OK", hosts_scanned=0))
        session.flush()
    return "r1"


def _finding(session, family, *, verdict="INSUFFICIENT_EVIDENCE", age=timedelta(days=1), title="slow ops on osd.2"):
    run_id = _ingest_run(session)
    session.add(LogFinding(cluster_id="c1", ingest_run_id=run_id, verdict=verdict, severity="warning", confidence=0.3, title=title,
                           summary="s", root_cause_hypothesis=None, fault_family=family, dedupe_key=f"{family}{age}{title}",
                           status="OPEN", created_at=NOW - age))
    session.flush()


def _case(session, code, *, confidence=0.9, diagnosis="osd.2 chậm"):
    incident = Incident(ceph_code=code, status="RESOLVED", detected_at=NOW, created_at=NOW)
    session.add(incident)
    session.flush()
    action = Action(incident_id=incident.id, action_id="investigate_manually", classification="SAFE", status="REJECTED",
                    target_nodes="[]", created_at=NOW)
    session.add(action)
    session.flush()
    session.add(RemediationCase(incident_id=incident.id, action_id=action.id, fault_family=code, evidence_fingerprint="f" * 64,
                                diagnosis=diagnosis, diagnosis_confidence=confidence, prompt_version="v1",
                                classification="SAFE", autonomy_decision="PENDING_APPROVAL", created_at=NOW))
    session.flush()
    return incident


def _queue(session, days=30):
    return evidence_gap_queue(session, family_codes=FAMILY_CODES, fault_codes=FAULTS, days=days, now=NOW)


def test_codes_map_to_the_most_specific_log_family():
    assert family_of_code("OSD_DOWN", FAMILY_CODES) == "network_heartbeat"
    assert family_of_code("OSD_NEARFULL", FAMILY_CODES) == "capacity_pressure"
    assert family_of_code("PG_DEGRADED", FAMILY_CODES) == "pg_peering"
    assert family_of_code("OSD_LATENCY_HIGH:3", FAMILY_CODES) == "bluestore_slow_ops"


def test_families_rank_by_missing_evidence_and_say_what_can_reproduce_them():
    session = _session()
    for _ in range(3):
        _finding(session, "bluestore_slow_ops")
    _finding(session, "pg_peering")
    _finding(session, "pg_peering", verdict="FINDING")
    _finding(session, None)  # unclassified: not reproducible on purpose
    _finding(session, "pg_peering", age=timedelta(days=40))  # outside the window
    _case(session, "PG_DEGRADED", confidence=0.3)
    _case(session, "OSD_DOWN", diagnosis="Chưa đủ bằng chứng để kết luận osd.1 down")
    _case(session, "OSD_DOWN", confidence=0.95)

    queue = {gap.family: gap for gap in _queue(session)}

    assert list(queue) == ["bluestore_slow_ops", "pg_peering", "network_heartbeat"]
    assert queue["pg_peering"].total == 2 and queue["pg_peering"].reproducible_by == ["osd_down_fault"]
    assert queue["bluestore_slow_ops"].reproducible_by == [] and "kiểu lỗi mới" in queue["bluestore_slow_ops"].next_step
    assert queue["network_heartbeat"].reproducible_by == ["osd_down_fault"]


def test_a_family_with_a_lab_label_is_marked_done():
    session = _session()
    _finding(session, "network_heartbeat")
    incident = _case(session, "OSD_DOWN")
    session.add(IncidentTimelineEvent(incident_id=incident.id, event_type="failure_lab_label", actor="failure-lab",
                                      evidence_json=json.dumps({"cause": "x"}), created_at=NOW))
    session.flush()

    [gap] = _queue(session)

    assert gap.lab_labels == 1 and gap.next_step == "đã có tái hiện trên lab"
