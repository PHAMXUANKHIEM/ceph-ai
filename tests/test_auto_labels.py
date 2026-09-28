import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import auto_labels as al
from shared.db import Base
from shared.models import Action, Incident, RemediationCase

NOW = datetime(2026, 9, 28, 12, 0)


def _facts(**overrides):
    base = dict(
        case_id="c", fault_family="OSD_DOWN", outcome="PROPOSED", regressed_1h=None, regressed_24h=None,
        incident_status="PENDING_APPROVAL", detected_at=NOW, resolved_at=None, executed=False,
        flapping=False, reopened_after_execution=False,
    )
    base.update(overrides)
    return al.CaseFacts(**base)


def test_each_labeling_function():
    assert al.label_case(_facts(outcome="VERIFIED_SUCCESS", regressed_24h=False, executed=True)).label == "CORRECT"
    assert al.label_case(_facts(regressed_1h=True, executed=True)).label == "INEFFECTIVE"
    assert al.label_case(_facts(outcome="EXECUTION_FAILED", executed=True)).label == "INEFFECTIVE"
    assert al.label_case(_facts(reopened_after_execution=True, executed=True)).label == "INEFFECTIVE"
    self_resolved = _facts(incident_status="RESOLVED", resolved_at=NOW + timedelta(minutes=10))
    assert al.label_case(self_resolved).label == "SELF_RESOLVED"
    assert al.label_case(_facts(flapping=True)).label == "FLAPPING"
    # Neither is claimed to be an operator-style false positive.
    assert "FALSE_POSITIVE" not in {al.label_case(self_resolved).label, al.label_case(_facts(flapping=True)).label}


def test_no_evidence_abstains_and_slow_recovery_is_not_a_false_alarm():
    assert al.label_case(_facts()).label is None
    slow = _facts(incident_status="RESOLVED", resolved_at=NOW + timedelta(hours=3))
    assert al.label_case(slow).label is None
    # An executed action whose incident then cleared is not a "self-resolved" false alarm.
    executed = _facts(incident_status="RESOLVED", resolved_at=NOW + timedelta(minutes=5), executed=True)
    assert al.label_case(executed).label is None


def test_conflicting_votes_are_weighted():
    label = al.label_case(_facts(outcome="VERIFIED_SUCCESS", regressed_24h=False, regressed_1h=True, executed=True))
    assert {vote.label for vote in label.votes} == {"CORRECT", "INEFFECTIVE"}
    assert label.label in {"CORRECT", "INEFFECTIVE"}
    assert label.confidence == 0.5


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _case(session, index, *, code, detected, resolved=None, status="RESOLVED", action_status="REJECTED",
          executed_at=None, evidence=None, verdict=None, outcome="PROPOSED"):
    incident = Incident(ceph_code=code, status=status, detected_at=detected, created_at=detected,
                        updated_at=resolved or detected, signal_evidence_json=json.dumps(evidence or {}))
    session.add(incident)
    session.flush()
    action = Action(incident_id=incident.id, action_id="restart_osd_daemon", classification="RISKY",
                    status=action_status, created_at=detected)
    session.add(action)
    session.flush()
    case = RemediationCase(
        incident_id=incident.id, action_id=action.id, fault_family=code.split(":")[0],
        evidence_fingerprint="f" * 64, prompt_version="v1", classification="RISKY",
        autonomy_decision="APPROVAL_REQUIRED", outcome=outcome, executed_at=executed_at,
        operator_verdict=verdict, created_at=detected,
    )
    session.add(case)
    session.flush()
    return case


def test_collect_facts_and_quality_report_against_operator_verdicts():
    session = _session()
    t0 = NOW - timedelta(days=2)
    _case(session, 1, code="NODE_UNREACHABLE:h1", detected=t0, resolved=t0 + timedelta(minutes=8),
          verdict="FALSE_POSITIVE")
    _case(session, 2, code="MON_DOWN", detected=t0, status="PENDING_APPROVAL", evidence={"flapping": True},
          verdict="CORRECT")
    executed = _case(session, 3, code="OSD_DOWN", detected=t0, action_status="EXECUTED",
                     executed_at=t0 + timedelta(minutes=1), outcome="EXECUTION_FAILED")
    # Same code reopens 2 hours after the execution.
    session.add(Incident(ceph_code="OSD_DOWN", status="RESOLVED", detected_at=t0 + timedelta(hours=2),
                         created_at=t0 + timedelta(hours=2)))
    session.flush()

    facts = {item.case_id: item for item in al.collect_facts(session, days=30, now=NOW)}
    assert facts[executed.id].reopened_after_execution is True
    report = al.quality_report(list(facts.values()))
    assert report["labelled"] == 3
    assert report["lf_precision_vs_operator"]["self_resolved_without_action"] == {"checked": 1, "precision": 1.0}
    assert report["lf_precision_vs_operator"]["flapping_signal"] == {"checked": 1, "precision": 0.0}
    assert report["lf_coverage"]["reopened_after_execution"] == 1


def test_auto_labels_never_touch_operator_verdicts():
    session = _session()
    case = _case(session, 1, code="MON_DOWN", detected=NOW - timedelta(days=1),
                 resolved=NOW - timedelta(days=1) + timedelta(minutes=2))
    al.quality_report(al.collect_facts(session, now=NOW))
    assert session.get(RemediationCase, case.id).operator_verdict is None
