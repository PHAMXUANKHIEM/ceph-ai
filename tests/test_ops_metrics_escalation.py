"""Ops report and engineer escalation (croit-parity C6)."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import engineer_escalation
from shared.models import Action, Base, Cluster, Incident, IncidentTimelineEvent, RemediationCase
from shared.ops_metrics import ops_report

FAMILIES = {"network_heartbeat": ("OSD_DOWN",), "capacity_pressure": ("OSD_NEARFULL",)}
NOW = datetime(2026, 10, 11, 12, 0)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([
        Cluster(id="prod", name="CS-LAB", ceph_mon_nodes="1", ssh_user="root", ssh_key_path="/k", is_default=True,
                autonomy_environment="production"),
        Cluster(id="lab", name="CS-STAGING", ceph_mon_nodes="2", ssh_user="root", ssh_key_path="/k",
                autonomy_environment="lab"),
    ])
    session.flush()
    return session


def _incident(session, code, *, cluster="prod", status="RESOLVED", detected_minutes_ago=600, closed_after=30,
              diagnosis="osd.3 down vì host mất mạng"):
    detected = NOW - timedelta(minutes=detected_minutes_ago)
    incident = Incident(cluster_id=cluster, ceph_code=code, status=status, detected_at=detected, created_at=detected,
                        updated_at=detected + timedelta(minutes=closed_after), diagnosis_text=diagnosis)
    session.add(incident)
    session.flush()
    return incident


def _case(session, incident, *, verdict=None, outcome="PROPOSED", confidence=0.9):
    action = Action(incident_id=incident.id, action_id="restart_osd_daemon", classification="SAFE",
                    status="EXECUTED", target_nodes="[]")
    session.add(action)
    session.flush()
    session.add(RemediationCase(incident_id=incident.id, action_id=action.id, cluster_id=incident.cluster_id,
                                fault_family=incident.ceph_code, evidence_fingerprint="f" * 64,
                                diagnosis=incident.diagnosis_text, diagnosis_confidence=confidence,
                                prompt_version="v1", classification="SAFE", autonomy_decision="PROPOSE",
                                outcome=outcome, operator_verdict=verdict, created_at=incident.detected_at))


def test_the_report_measures_production_families_only(session):
    a = _incident(session, "OSD_DOWN", closed_after=20, status="AUTO_FIXED")
    b = _incident(session, "OSD_DOWN", closed_after=40)
    _incident(session, "OSD_DOWN", status="PENDING_APPROVAL")
    _case(session, a, verdict="CORRECT", outcome="VERIFIED_SUCCESS")
    _case(session, b, verdict="FALSE_POSITIVE", outcome="EXECUTION_FAILED")
    _incident(session, "OSD_DOWN", cluster="lab")              # staging: not production experience
    _incident(session, "POOL_CREATE_REQUEST")                   # operator request: no fault family
    session.commit()

    [row] = ops_report(session, family_codes=FAMILIES, days=30, now=NOW)

    assert row.as_dict() == {
        "family": "network_heartbeat", "incidents": 3, "open": 1, "closed": 2, "auto_fixed": 1,
        "auto_fixed_share": 0.5, "mttr_minutes_estimate": 30.0, "diagnosis_accuracy": 0.5, "diagnoses_judged": 2,
        "fix_success": 0.5, "fixes_verified": 2}


@pytest.mark.parametrize(("code", "status", "diagnosis", "confidence", "reason"), [
    ("OSD_DOWN", "FAILED", "osd.3 down", 0.9, "thất bại"),
    ("MDS_CACHE_OVERSIZED", "PENDING_APPROVAL", "mds cache lớn", 0.9, "không thuộc họ lỗi"),
    ("OSD_DOWN", "DIAGNOSING", None, None, "chưa chẩn đoán"),
    ("OSD_DOWN", "PENDING_APPROVAL", "osd.3 down", 0.3, "độ tin chẩn đoán chỉ 30%"),
    ("OSD_DOWN", "PENDING_APPROVAL", "Chưa đủ bằng chứng để kết luận", 0.8, "chưa đủ bằng chứng"),
])
def test_incidents_outside_known_ground_are_escalated_once(session, code, status, diagnosis, confidence, reason):
    incident = _incident(session, code, status=status, detected_minutes_ago=45, diagnosis=diagnosis)
    if confidence is not None:
        _case(session, incident, confidence=confidence)
    sent = []
    enqueue = lambda session, **kwargs: sent.append(kwargs)  # noqa: E731

    first = engineer_escalation.escalate_due(session, family_codes=FAMILIES, now=NOW, enqueue=enqueue)
    second = engineer_escalation.escalate_due(session, family_codes=FAMILIES, now=NOW, enqueue=enqueue)

    assert first == [incident.id] and second == [] and len(sent) == 1
    assert reason in sent[0]["message"] and "CẦN KỸ SƯ" in sent[0]["message"]
    assert session.query(IncidentTimelineEvent).filter_by(event_type="engineer_escalation").count() == 1


@pytest.mark.parametrize(("code", "cluster", "minutes", "diagnosis"), [
    ("OSD_DOWN", "prod", 45, "osd.3 down vì host mất mạng"),       # known ground, confident
    ("OSD_DOWN", "prod", 10, None),                                  # too new
    ("OSD_DOWN", "lab", 45, None),                                   # staging is the lab's business
    ("CLUSTER_NODE_ADD", "prod", 45, None),                          # operator request
])
def test_known_new_staging_or_operator_incidents_are_not_escalated(session, code, cluster, minutes, diagnosis):
    incident = _incident(session, code, cluster=cluster, status="PENDING_APPROVAL", detected_minutes_ago=minutes,
                         diagnosis=diagnosis)
    if diagnosis:
        _case(session, incident, confidence=0.9)

    assert engineer_escalation.escalate_due(session, family_codes=FAMILIES, now=NOW, enqueue=lambda s, **k: None) == []


def test_the_ops_report_page_renders(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    page = dashboard_client.get("/ops-report?days=7").text
    assert "Báo cáo vận hành theo họ lỗi" in page and "7 ngày gần nhất" in page
