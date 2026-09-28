import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import decision_log
from shared.db import Base
from shared.deterministic_triage import Triage
from shared.models import Action, AutonomyDecision, Incident, RemediationCase

NOW = datetime(2026, 9, 28, 12, 0)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _case(session, code="MON_CLOCK_SKEW", verdict=None, outcome="PROPOSED", regressed=None, fp="a"):
    incident = Incident(ceph_code=code, status="PENDING_APPROVAL", severity="HEALTH_WARN", detected_at=NOW)
    session.add(incident)
    session.flush()
    action = Action(incident_id=incident.id, action_id="resync_ntp", classification="SAFE", status="PENDING")
    session.add(action)
    session.flush()
    case = RemediationCase(incident_id=incident.id, action_id=action.id, fault_family=code,
                           evidence_fingerprint=fp * 64, prompt_version="v1", classification="SAFE",
                           autonomy_decision="AUTO_EXECUTE", outcome=outcome, operator_verdict=verdict,
                           regressed_24h=regressed, diagnosis_confidence=0.8)
    session.add(case)
    session.flush()
    return incident, case


ENVELOPE = {"nodes": ["10.0.0.1", "10.0.0.2"], "cluster_snapshot": {"health": {"status": "HEALTH_WARN"}},
            "log_excerpt": "password=hunter2 secret text"}


def test_record_logs_structured_context_only_and_is_idempotent():
    session = _session()
    incident, case = _case(session, code="MON_CLOCK_SKEW:mon-b")
    triage = Triage("MON_CLOCK_SKEW", "MON_SKEWED", confidence=0.8)
    first = decision_log.record(session, incident=incident, case=case, chosen_action="resync_ntp",
                                chosen_by="rules", envelope=ENVELOPE, triage=triage, now=NOW)
    again = decision_log.record(session, incident=incident, case=case, chosen_action="resync_ntp",
                                chosen_by="llm", envelope=ENVELOPE, now=NOW)
    assert first is again and session.query(AutonomyDecision).count() == 1
    context = json.loads(first.context_json)
    assert context == {
        "fault_family": "MON_CLOCK_SKEW", "severity": "HEALTH_WARN", "cluster_status": "HEALTH_WARN",
        "affected_nodes": 2, "hour_utc": 12, "diagnosis_confidence": 0.8, "shadow_trust_score": None,
        "shadow_sample_count": None, "classification": "SAFE", "triage_conclusion": "MON_SKEWED",
        "triage_confidence": 0.8,
    }
    assert "hunter2" not in first.context_json and "10.0.0.1" not in first.context_json
    assert json.loads(first.candidates_json) == ["investigate_manually", "resync_ntp"]
    assert (first.propensity, first.chosen_by, first.policy_version) == (1.0, "rules", decision_log.POLICY_VERSION)
    with pytest.raises(ValueError):
        decision_log.record(session, incident=incident, case=case, chosen_action="x", chosen_by="magic",
                            envelope={})


@pytest.mark.parametrize("verdict,outcome,regressed,reward", [
    ("CORRECT", "PROPOSED", None, 1.0), ("UNSAFE", "VERIFIED_SUCCESS", False, 0.0),
    ("FALSE_POSITIVE", None, None, 0.0), ("INCONCLUSIVE", "VERIFIED_SUCCESS", False, None),
    (None, "VERIFIED_SUCCESS", False, 1.0), (None, "VERIFIED_SUCCESS", True, 0.0),
    (None, "EXECUTION_FAILED", None, 0.0), (None, "PROPOSED", None, None),
])
def test_reward_prefers_the_operator_then_the_verified_outcome(verdict, outcome, regressed, reward):
    assert decision_log.reward_for(verdict, outcome, regressed) == reward


def test_load_joins_current_rewards_and_counts_unknown():
    session = _session()
    for index, (verdict, outcome) in enumerate([("CORRECT", "PROPOSED"), (None, "PROPOSED"), (None, "VERIFIED_FAILED")]):
        incident, case = _case(session, code=f"MON_CLOCK_SKEW:mon-{index}", verdict=verdict, outcome=outcome,
                               fp=str(index))
        decision_log.record(session, incident=incident, case=case, chosen_action="resync_ntp", chosen_by="llm",
                            envelope=ENVELOPE, now=NOW)
    session.flush()
    for row in session.query(AutonomyDecision):
        row.created_at = NOW - timedelta(days=1)
    session.commit()
    logged, unknown = decision_log.load(session, days=30, now=NOW)
    assert sorted(item.reward for item in logged) == [0.0, 1.0] and unknown == 1
