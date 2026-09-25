import json
from datetime import datetime

from shared.autopilot_guardrails import evaluate_guardrails
from config.settings import settings
from shared.models import Action, Cluster, Incident, RemediationCase, RemediationRuntimeDecision
from shared.remediation_runtime import evaluate_and_persist
from shared.remediation_state_machine import RemediationState, persist_transition, recover_after_worker_restart


def _cluster():
    return Cluster(
        name="runtime-cluster", ceph_mon_nodes="mon-a", ssh_user="root",
        ssh_key_path="/tmp/test-key", autopilot_enabled=True,
        autopilot_mode="LIMITED_AUTOPILOT", is_active=True,
    )


def _action(session, cluster):
    incident = Incident(cluster_id=cluster.id, ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow())
    session.add(incident)
    session.flush()
    action = Action(
        incident_id=incident.id, action_id="resync_ntp", classification="SAFE",
        status="PENDING", target_nodes='["mon-a"]', remediation_state="PROPOSED",
    )
    session.add(action)
    session.flush()
    case = RemediationCase(
        incident_id=incident.id, action_id=action.id, cluster_id=cluster.id,
        fault_family="OSD_DOWN", evidence_fingerprint="a" * 64,
        prompt_version="test", classification="SAFE", autonomy_decision="AUTO_EXECUTE",
        playbook_version="3", outcome="PROPOSED",
    )
    session.add(case)
    session.flush()
    return incident, action, case


def test_runtime_decision_is_persisted_and_budget_exhaustion_blocks(db_session, monkeypatch):
    cluster = _cluster()
    db_session.add(cluster)
    db_session.flush()
    _incident, action, _case = _action(db_session, cluster)
    db_session.commit()
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr("shared.remediation_runtime.settings.autopilot_max_actions_per_hour", 0)

    decision = evaluate_and_persist(
        db_session, action=action, cluster=cluster,
        incident_id=action.incident_id, health_status="HEALTH_OK",
    )
    assert not decision.allowed
    assert "budget" in decision.reason
    row = db_session.query(type(action)).filter_by(id=action.id).one()
    assert db_session.query(RemediationRuntimeDecision).count() == 1
    assert row.remediation_state == "PROPOSED"


def test_partial_success_can_become_failed_then_rollback_without_retry(db_session):
    cluster = _cluster()
    db_session.add(cluster)
    db_session.flush()
    incident, action, case = _action(db_session, cluster)
    assert persist_transition(db_session, action=action, incident=incident, case=case, requested="APPROVED").allowed
    assert persist_transition(db_session, action=action, incident=incident, case=case, requested="EXECUTING", lock_owner="w1", active_lock_owner="w1").allowed
    assert persist_transition(db_session, action=action, incident=incident, case=case, requested="FAILED").allowed
    rollback = persist_transition(db_session, action=action, incident=incident, case=case, requested="ROLLED_BACK")
    assert rollback.allowed
    assert action.remediation_state == RemediationState.ROLLED_BACK.value
    assert case.outcome == "ROLLED_BACK"


def test_split_brain_and_provider_failure_are_fail_closed():
    split_brain = recover_after_worker_restart("EXECUTING", lock_present=True)
    assert not split_brain.allowed
    provider_unavailable = evaluate_guardrails(type("Context", (), {
        "mode": "LIMITED_AUTOPILOT", "kill_switch": True, "cluster_enabled": True,
        "action_id": "resync_ntp", "classification": "SAFE", "allowlisted": True,
        "target_count": 1, "max_targets": 2, "actions_used": 0, "action_budget": 5,
        "cooldown_active": False, "health_status": "UNKNOWN",
        "maintenance_window_open": True,
    })())
    assert not provider_unavailable.allowed
    assert "health" in provider_unavailable.reason
