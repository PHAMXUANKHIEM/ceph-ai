import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.failure_lab_report import STAGES, family_summary_text, learning_report
from shared.models import Base, Cluster, Incident, IncidentTimelineEvent, RemediationCase

FAMILIES = {"network_heartbeat": ("OSD_DOWN",), "capacity_pressure": ("OSD_NEARFULL",)}
T0 = datetime(2026, 10, 1, 3, 0)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _cluster(session, name, environment):
    cluster = Cluster(name=name, ceph_mon_nodes="10.0.0.1", ssh_user="root", ssh_key_path="/k",
                      autonomy_environment=environment)
    session.add(cluster)
    session.flush()
    return cluster.id


def _label(session, cluster_id, code, at, *, diagnosis, seconds=None, passed=None):
    incident = Incident(ceph_code=code, status="RESOLVED", cluster_id=cluster_id, detected_at=at, created_at=at)
    session.add(incident)
    session.flush()
    stages = {stage: True for stage in STAGES} | {"diagnosis": diagnosis}
    session.add(IncidentTimelineEvent(
        incident_id=incident.id, event_type="failure_lab_label", actor="failure-lab", created_at=at,
        evidence_json=json.dumps({"stages": stages, "passed": all(stages.values()) if passed is None else passed,
                                  "diagnosis_seconds": seconds})))


def _case(session, cluster_id, code, at, *, confidence):
    incident = Incident(ceph_code=code, status="RESOLVED", cluster_id=cluster_id, detected_at=at, created_at=at)
    session.add(incident)
    session.flush()
    from shared.models import Action

    action = Action(incident_id=incident.id, action_id="investigate_manually", classification="SAFE",
                    status="EXECUTED", target_nodes="[]")
    session.add(action)
    session.flush()
    session.add(RemediationCase(incident_id=incident.id, action_id=action.id, cluster_id=cluster_id, fault_family=code,
                                evidence_fingerprint="f" * 64, diagnosis="osd down", diagnosis_confidence=confidence,
                                prompt_version="v1", classification="SAFE", autonomy_decision="PROPOSE",
                                created_at=at))


def test_report_counts_runs_until_a_correct_diagnosis_and_compares_before_and_after_lab():
    session = _session()
    lab, prod = _cluster(session, "STG", "lab"), _cluster(session, "PROD", "production")
    _label(session, lab, "OSD_DOWN", T0, diagnosis=False, seconds=300)
    _label(session, lab, "OSD_DOWN", T0 + timedelta(days=1), diagnosis=True, seconds=120)
    _label(session, lab, "OSD_DOWN", T0 + timedelta(days=2), diagnosis=True, seconds=60)
    for days, confidence in ((-5, 0.3), (-4, 0.2), (-3, 0.9), (3, 0.8), (4, 0.9), (5, 0.4)):
        _case(session, prod, "OSD_DOWN", T0 + timedelta(days=days), confidence=confidence)
    _case(session, lab, "OSD_DOWN", T0 + timedelta(days=6), confidence=0.1)  # lab cases never count as production
    session.commit()

    [item] = learning_report(session, family_codes=FAMILIES)

    assert (item.family, len(item.runs), item.passed, item.first_correct_run) == ("network_heartbeat", 3, 2, 2)
    assert item.median_correct_diagnosis_seconds == 90
    assert item.lab_accuracy == ((0, 1), (2, 2))
    assert (item.production_before, item.production_after) == ((2, 3), (1, 3))
    assert item.as_dict()["stage_rates"]["diagnosis"] == 0.667
    text = family_summary_text(item)
    assert "3 lượt, 2 đạt đủ 6 khâu" in text and "lượt: 2" in text and "90 s" in text
    assert "lượt đầu 0% (0/1) · có tham khảo lab 100% (2/2)" in text
    assert "trước lab 67% (2/3) · sau lab 33% (1/3)" in text


def test_families_without_lab_runs_are_not_reported_and_empty_values_read_plainly():
    session = _session()
    lab = _cluster(session, "STG", "lab")
    _label(session, lab, "OSD_NEARFULL", T0, diagnosis=False)
    _label(session, lab, "UNMAPPED_CODE", T0, diagnosis=True)
    session.commit()

    [item] = learning_report(session, family_codes=FAMILIES)

    assert item.family == "capacity_pressure" and item.first_correct_run is None
    text = family_summary_text(item)
    assert "lượt: chưa có" in text and "(trung vị): chưa có" in text and "trước lab chưa có" in text
