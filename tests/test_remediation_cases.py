import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.models import Action, Incident, RemediationCase
from shared.remediation_cases import (
    backfill_missing_cases, create_for_action, record_execution, record_inconclusive,
    record_verified,
)


def _session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(session):
    detected = datetime.utcnow() - timedelta(seconds=30)
    incident = Incident(ceph_code="OSD_DOWN", status="DIAGNOSING", detected_at=detected)
    session.add(incident); session.flush()
    action = Action(
        incident_id=incident.id, action_id="restart_osd_daemon",
        classification="RISKY", status="PENDING", target_nodes='["node-a"]',
    )
    session.add(action); session.flush()
    return incident, action


def test_case_freezes_redacted_pre_state_and_has_stable_fingerprint():
    session = _session()
    incident, action = _seed(session)
    envelope = {
        "nodes": ["node-a"], "ceph_exec_mode": "cephadm",
        "cluster_snapshot": {"ceph_version": "18.2.4", "checks": {"OSD_DOWN": {}}},
    }
    first = create_for_action(
        session, incident=incident, action=action, redacted_envelope=envelope,
        diagnosis="osd stopped", model_provider="9router",
    )
    second = create_for_action(
        session, incident=incident, action=action, redacted_envelope=envelope,
        diagnosis="must not duplicate", model_provider="9router",
    )
    session.commit()

    assert first.id == second.id
    assert session.query(RemediationCase).count() == 1
    assert len(first.evidence_fingerprint) == 64
    assert json.loads(first.pre_state_json)["ceph_version"] == "18.2.4"
    assert first.autonomy_decision == "PENDING_APPROVAL"
    assert first.outcome == "PROPOSED"


def test_case_outcome_requires_telemetry_verification():
    session = _session()
    incident, action = _seed(session)
    case = create_for_action(
        session, incident=incident, action=action,
        redacted_envelope={"nodes": ["node-a"], "cluster_snapshot": {"status": "HEALTH_WARN"}},
        diagnosis="osd stopped", model_provider="9router",
    )
    executed = datetime.utcnow()
    record_execution(session, action_id=action.id, succeeded=True, executed_at=executed)
    assert case.outcome == "EXECUTED_PENDING_VERIFY"

    verified = executed + timedelta(seconds=10)
    record_verified(
        session, incident_id=incident.id, succeeded=True, verified_at=verified,
        post_state={"status": "HEALTH_OK"},
    )
    assert case.outcome == "VERIFIED_SUCCESS"
    assert case.verified_at == verified
    assert json.loads(case.post_state_json) == {"status": "HEALTH_OK"}
    assert case.recovery_seconds >= 0


def test_inconclusive_case_records_no_retry_evidence():
    session = _session()
    incident, action = _seed(session)
    case = create_for_action(
        session, incident=incident, action=action,
        redacted_envelope={"nodes": ["node-a"], "cluster_snapshot": {}},
        diagnosis="osd stopped", model_provider="9router",
    )
    now = datetime.utcnow()
    record_inconclusive(session, action_id=action.id, at=now, reason="lease expired")
    assert case.outcome == "INCONCLUSIVE"
    assert json.loads(case.side_effects_json) == {"auto_retry": False, "reason": "lease expired"}


def test_backfill_covers_non_ai_actions_without_trusting_legacy_resolved_rows():
    session = _session()
    incident, action = _seed(session)
    incident.status = "RESOLVED"
    incident.signal_evidence_json = '{"status":"HEALTH_WARN"}'
    action.status = "EXECUTED"
    action.executed_at = datetime.utcnow()
    session.commit()

    assert backfill_missing_cases(session, limit=200) == 1
    case = session.query(RemediationCase).filter_by(action_id=action.id).one()
    assert case.outcome == "LEGACY_RESOLVED_UNVERIFIED"
    assert case.prompt_version == "legacy-backfill-v1"
    assert json.loads(case.side_effects_json)["trust_eligible"] is False
    assert backfill_missing_cases(session, limit=200) == 0


def test_backfill_is_bounded_and_preserves_pending_verify_state():
    session = _session()
    for index in range(3):
        incident, action = _seed(session)
        incident.ceph_code = f"OSD_DOWN_{index}"
        incident.status = "VERIFYING"
        action.status = "EXECUTED"
        action.executed_at = datetime.utcnow()
    session.commit()

    assert backfill_missing_cases(session, limit=2) == 2
    assert session.query(RemediationCase).count() == 2
    assert backfill_missing_cases(session, limit=2) == 1
    assert {row.outcome for row in session.query(RemediationCase)} == {"EXECUTED_PENDING_VERIFY"}
