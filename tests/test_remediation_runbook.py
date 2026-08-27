import asyncio
from datetime import datetime, timedelta

import pytest

from shared.models import Action, Cluster, Incident, RemediationCase
from shared.remediation_runbook import RunbookError, build_source, generate, to_markdown, validate


def _seed(session):
    now = datetime(2026, 8, 27, 10, 0)
    cluster = Cluster(
        id="cluster-1", name="lab", ceph_mon_nodes="10.0.0.1",
        ssh_user="ceph", ssh_key_path="/tmp/key", is_default=True,
    )
    incident = Incident(
        id="incident-1", cluster_id=cluster.id, ceph_code="OSD_DOWN", status="RESOLVED",
        severity="HEALTH_ERR", detected_at=now,
        signal_evidence_json='{"osd_id": 3, "api_token": "hidden"}',
    )
    action = Action(
        id="action-1", incident_id=incident.id, action_id="restart_osd_daemon",
        classification="RISKY", status="EXECUTED", rationale="Restart the failed OSD",
        created_at=now + timedelta(minutes=1),
    )
    case = RemediationCase(
        id="case-1", incident_id=incident.id, action_id=action.id, cluster_id=cluster.id,
        fault_family="OSD_DOWN", evidence_fingerprint="f" * 64, diagnosis="OSD stopped",
        diagnosis_confidence=0.92, prompt_version="v1", classification="RISKY",
        autonomy_decision="PENDING_APPROVAL", outcome="VERIFIED_SUCCESS",
        verified_at=now + timedelta(minutes=5), recovery_seconds=300,
        pre_state_json='{"health": "ERR", "access_token": "hidden"}',
        post_state_json='{"health": "OK"}',
    )
    session.add(cluster)
    session.commit()
    session.add(incident)
    session.commit()
    session.add(action)
    session.commit()
    session.add(case)
    session.commit()


def test_build_source_is_bounded_redacted_and_citable(db_session):
    _seed(db_session)
    source = build_source(db_session, fault_family="OSD_DOWN", cluster_id="cluster-1")
    assert source["case_count"] == 1
    case = source["cases"][0]
    assert case["pre_state"]["access_token"] == "[REDACTED]"
    assert case["evidence_ids"] == ["case:case-1", "incident:incident-1", "action:action-1"]


def test_source_excludes_synthetic_case(db_session):
    _seed(db_session)
    incident = db_session.get(Incident, "incident-1")
    incident.signal_evidence_json = '{"synthetic_injection": true}'
    db_session.commit()
    with pytest.raises(RunbookError, match="VERIFIED_SUCCESS"):
        build_source(db_session, fault_family="OSD_DOWN", cluster_id="cluster-1")


def test_validate_rejects_invented_citation_and_markdown_is_copyable(db_session):
    _seed(db_session)
    source = build_source(db_session, fault_family="OSD_DOWN", cluster_id="cluster-1")
    report = {
        "title": "OSD down recovery", "when_to_use": "When OSD is down",
        "prechecks": ["Check evidence"], "steps": ["Restart the failed OSD"],
        "verification": ["Confirm health is OK"], "rollback": ["Stop and escalate"],
        "prevention": ["Monitor OSD"], "limitations": "Only one verified case.",
        "citations": ["case:case-1"],
    }
    validated = validate(report, source)
    assert "# OSD down recovery" in to_markdown(validated)
    report["citations"] = ["invented:event"]
    with pytest.raises(RunbookError, match="citation"):
        validate(report, source)


def test_generate_calls_and_validates_model(db_session, monkeypatch):
    _seed(db_session)
    source = build_source(db_session, fault_family="OSD_DOWN", cluster_id="cluster-1")
    result = {
        "title": "OSD down recovery", "when_to_use": "When OSD is down",
        "prechecks": ["Check evidence"], "steps": ["Restart the failed OSD"],
        "verification": ["Confirm health is OK"], "rollback": ["Escalate"],
        "prevention": ["Monitor OSD"], "limitations": "Limited sample.",
        "citations": ["case:case-1"],
    }

    async def fake_model(_source):
        return result

    monkeypatch.setattr("shared.remediation_runbook._call_model", fake_model)
    assert asyncio.run(generate(source))["source_case_count"] == 1
