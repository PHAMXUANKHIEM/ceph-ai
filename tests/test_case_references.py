"""Reference cases and proposal history for the diagnosis prompt (shared/case_references.py)."""

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import audit, case_references
from shared.db import Base
from shared.models import Action, AuditEntry, Incident, RemediationCase

NOW = datetime(2026, 10, 7, 12, 0)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _case(session, code, *, playbook="investigate_manually", outcome="REJECTED", verdict=None, status="RESOLVED",
          diagnosis="chẩn đoán cũ", nodes=("10.0.0.1",), cluster="c1", minutes=12, age=timedelta(days=1),
          version="18.2.4"):
    detected = NOW - age
    incident = Incident(ceph_code=code, status=status, cluster_id=cluster, detected_at=detected, created_at=detected,
                        updated_at=detected + timedelta(minutes=minutes))
    session.add(incident)
    session.flush()
    action = Action(incident_id=incident.id, action_id=playbook, classification="RISKY", status="REJECTED",
                    target_nodes="[]", created_at=detected)
    session.add(action)
    session.flush()
    nodes_json = '{"nodes": [%s]}' % ", ".join(f'"{node}"' for node in nodes)
    case = RemediationCase(incident_id=incident.id, action_id=action.id, cluster_id=cluster, fault_family=code,
                           entity_keys_json=nodes_json, outcome=outcome, operator_verdict=verdict,
                           diagnosis=diagnosis, ceph_version=version, deployment_mode="cephadm",
                           evidence_fingerprint="f" * 64,
                           prompt_version="incident-diagnosis-v1", playbook_version="unregistered",
                           classification="RISKY", autonomy_decision="PENDING_APPROVAL", created_at=detected)
    session.add(case)
    session.flush()
    return incident, action, case


def _find(session, code="NODE_RESOURCE_HIGH:10.0.0.9", **overrides):
    values = {"incident_id": "new", "cluster_id": "c1", "include_unscoped": False, "ceph_code": code,
              "nodes": ["10.0.0.9"], "ceph_version": "18.2.7", "deployment_mode": "cephadm"}
    values.update(overrides)
    return case_references.find_reference_cases(session, **values)


def test_cases_of_the_same_family_are_found_although_the_entity_differs():
    session = _session()
    _case(session, "NODE_RESOURCE_HIGH:10.0.0.1", verdict="CORRECT", diagnosis="CPU cao do scrub, tự hạ")
    _case(session, "NODE_UNREACHABLE:10.0.0.1", verdict="CORRECT")  # another family
    session.commit()

    references = _find(session)

    assert [(item["kind"], item["diagnosis"]) for item in references] == [
        ("diagnosis_confirmed", "CPU cao do scrub, tự hạ")]


def test_kinds_rank_confirmed_above_self_resolved_and_drop_bad_or_recurring_cases():
    session = _session()
    _case(session, "OSD_LATENCY_HIGH:3", diagnosis="tự hết A")
    _case(session, "OSD_LATENCY_HIGH:4", diagnosis="tự hết B")
    _case(session, "OSD_LATENCY_HIGH:5", verdict="CORRECT", diagnosis="đúng")
    _case(session, "OSD_LATENCY_HIGH:6", verdict="FALSE_POSITIVE", diagnosis="sai")
    recurring, _action, _case_row = _case(session, "OSD_LATENCY_HIGH:7", diagnosis="tái phát")
    session.add(AuditEntry(incident_id=recurring.id, event_type=audit.EVENT_INCIDENT_RECURRED, actor="system"))
    _case(session, "OSD_LATENCY_HIGH:8", status="PENDING_APPROVAL", diagnosis="còn mở")
    session.commit()

    references = _find(session, code="OSD_LATENCY_HIGH:9")

    assert [item["kind"] for item in references] == ["diagnosis_confirmed", "self_resolved"]  # one self-resolved
    assert {item["diagnosis"] for item in references}.isdisjoint({"sai", "tái phát", "còn mở"})
    assert references[1]["resolved_after_minutes"] == 12


def test_other_clusters_and_other_ceph_majors_are_excluded_but_legacy_rows_follow_the_default():
    session = _session()
    _case(session, "OSD_DOWN", verdict="CORRECT", cluster="c2", diagnosis="cụm khác")
    _case(session, "OSD_DOWN", verdict="CORRECT", version="17.2.6", diagnosis="bản khác")
    _case(session, "OSD_DOWN", verdict="CORRECT", cluster=None, diagnosis="dòng cũ")
    session.commit()

    assert _find(session, code="OSD_DOWN") == []
    assert [item["diagnosis"] for item in _find(session, code="OSD_DOWN", include_unscoped=True)] == ["dòng cũ"]


def test_a_reboot_that_was_never_needed_shows_up_in_the_history_block():
    session = _session()
    for index in range(4):
        _incident, action, _case_row = _case(session, f"NODE_RESOURCE_HIGH:10.0.0.{index}", playbook="hard_reboot_node")
        session.add(AuditEntry(incident_id=action.incident_id, action_id=action.id, actor="system:watcher",
                               event_type=audit.EVENT_RISKY_ACTION_AUTO_CANCELLED_INCIDENT_RESOLVED))
    _incident, rejected, _case_row = _case(session, "NODE_RESOURCE_HIGH:10.0.0.9", playbook="hard_reboot_node")
    session.add(AuditEntry(incident_id=rejected.incident_id, action_id=rejected.id, actor="admin",
                           event_type=audit.EVENT_RISKY_ACTION_REJECTED))
    _case(session, "NODE_RESOURCE_HIGH:10.0.0.8", playbook="restart_osd_daemon")  # proposed once: not flagged
    _case(session, "NODE_RESOURCE_HIGH:10.0.0.7", playbook="hard_reboot_node", age=timedelta(days=40))  # too old
    session.commit()

    history = case_references.proposal_history(session, cluster_id="c1", include_unscoped=False,
                                               ceph_code="NODE_RESOURCE_HIGH:10.0.0.5", now=NOW)

    assert history == [{"action_id": "hard_reboot_node", "proposed": 5, "self_resolved": 4, "operator_rejected": 1}]
    block = case_references.history_block(history)
    assert "hard_reboot_node: đề xuất 5 lần; sự cố tự hết trước khi duyệt 4 lần; operator từ chối 1 lần" in block
    assert "chọn investigate_manually" in block


def test_an_unexecuted_proposal_is_never_shown_as_the_fix_and_duplicates_collapse():
    session = _session()
    for _ in range(3):
        _case(session, "NODE_RESOURCE_HIGH:10.0.0.1", playbook="hard_reboot_node", verdict="CORRECT",
              diagnosis="CPU cao tạm thời")
    _case(session, "NODE_RESOURCE_HIGH:10.0.0.2", verdict="CORRECT", diagnosis="")
    session.commit()

    references = _find(session)
    block = case_references.references_block(references)

    assert len(references) == 1
    assert "(đã đề xuất hard_reboot_node nhưng KHÔNG chạy): CPU cao tạm thời" in block
    assert "→ hard_reboot_node" not in block


def test_blocks_are_empty_without_data():
    assert case_references.references_block([]) == "" and case_references.history_block([]) == ""
