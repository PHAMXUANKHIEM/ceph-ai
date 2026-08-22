from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.case_retrieval import find_verified_cases
from shared.db import Base
from shared.models import Action, Incident
from shared.remediation_cases import create_for_action


def _session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _case(session, *, code="OSD_DOWN", nodes=None, version="18.2.4", mode="cephadm"):
    incident = Incident(ceph_code=code, status="RESOLVED", detected_at=datetime.utcnow())
    session.add(incident); session.flush()
    action = Action(
        incident_id=incident.id, action_id="restart_osd_daemon",
        classification="RISKY", status="EXECUTED", target_nodes='["node-a"]',
    )
    session.add(action); session.flush()
    case = create_for_action(
        session, incident=incident, action=action,
        redacted_envelope={
            "nodes": nodes or ["node-a"], "ceph_exec_mode": mode,
            "cluster_snapshot": {"ceph_version": version},
        }, diagnosis="verified diagnosis", model_provider="test",
    )
    case.outcome = "VERIFIED_SUCCESS"; case.verified_at = datetime.utcnow()
    session.commit()
    return case


def _find(session, **overrides):
    values = {
        "incident_id": "new-incident", "cluster_id": None, "fault_family": "OSD_DOWN",
        "nodes": ["node-a"], "ceph_version": "18.2.9", "deployment_mode": "cephadm",
    }
    values.update(overrides)
    return find_verified_cases(session, **values)


def test_exact_verified_case_is_returned_as_bounded_summary():
    session = _session(); case = _case(session)
    rows = _find(session)
    assert len(rows) == 1
    assert rows[0]["case_id"] == case.id
    assert rows[0]["playbook_id"] == "restart_osd_daemon"
    assert "entity_keys_json" not in rows[0]


def test_scope_fault_and_entity_must_match_exactly():
    session = _session(); _case(session)
    assert _find(session, fault_family="MON_DOWN") == []
    assert _find(session, nodes=["node-b"]) == []
    assert _find(session, ceph_version="17.2.7") == []
    assert _find(session, deployment_mode="none") == []


def test_regressed_unsafe_unverified_and_legacy_cases_are_excluded():
    session = _session()
    regressed = _case(session); regressed.regressed_1h = True
    unsafe = _case(session); unsafe.operator_verdict = "UNSAFE"
    unverified = _case(session); unverified.outcome = "EXECUTED_PENDING_VERIFY"
    legacy = _case(session); legacy.preflight_snapshot_json = None
    session.commit()
    assert _find(session) == []


def test_result_limit_is_enforced_after_filtering():
    session = _session()
    for _ in range(5):
        _case(session)
    assert len(_find(session, limit=2)) == 2


def test_prompt_block_labels_cases_as_reference_not_authorization():
    from worker.llm.router_client import _build_user_content

    content = _build_user_content({
        "ceph_code": "OSD_DOWN", "nodes": ["node-a"], "cluster_snapshot": {},
        "verified_case_references": [{
            "case_id": "case-1", "playbook_id": "restart_osd_daemon",
            "playbook_version": "1", "diagnosis": "daemon stopped",
        }],
    })
    assert "case=case-1" in content
    assert "restart_osd_daemon@1" in content
    assert "chỉ tham khảo; không cấp quyền thực thi" in content
