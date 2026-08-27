import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.change_risk import assess, assess_and_record, attach_summary
from shared.db import Base
from shared.models import Action, ChangeRiskAssessment, Incident, RemediationCase


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _action(session, suffix, *, action_id="restart_osd_daemon", cluster_id=None):
    incident = Incident(
        ceph_code="OSD_DOWN", status="DIAGNOSING", detected_at=datetime.utcnow(),
        cluster_id=cluster_id,
    )
    session.add(incident); session.flush()
    action = Action(
        incident_id=incident.id, action_id=action_id, classification="SAFE",
        status="PENDING", rationale="restart daemon",
    )
    session.add(action); session.flush()
    return incident, action


def _case(session, suffix, outcome, *, regression=False, action_id="restart_osd_daemon", cluster_id=None):
    incident, action = _action(session, suffix, action_id=action_id, cluster_id=cluster_id)
    case = RemediationCase(
        incident_id=incident.id, action_id=action.id, cluster_id=cluster_id, fault_family="OSD_DOWN",
        evidence_fingerprint=(suffix * 64)[:64], prompt_version="test",
        classification="SAFE", autonomy_decision="AUTO_EXECUTE",
        playbook_version="1", outcome=outcome, verified_at=datetime.utcnow(),
        regressed_24h=regression,
    )
    session.add(case); session.flush()
    return case


def test_regression_blocks_autopilot_and_records_only_case_ids():
    session = _session()
    prior = _case(session, "a", "VERIFIED_SUCCESS", regression=True)
    incident, action = _action(session, "current")

    result = assess_and_record(session, action=action, incident=incident)
    attach_summary(action, result)
    session.commit()

    assert result.level == "HIGH" and result.blocks_autopilot is True
    row = session.query(ChangeRiskAssessment).filter_by(action_id=action.id).one()
    assert row.regression_count == 1
    assert json.loads(row.evidence_json)["regression_case_ids"] == [prior.id]
    assert "[Change-risk analyzer]" in action.rationale


def test_three_clean_successes_are_low_risk():
    session = _session()
    for suffix in "abc":
        _case(session, suffix, "VERIFIED_SUCCESS")
    incident, action = _action(session, "current")

    result = assess(session, action=action, incident=incident)

    assert result.level == "LOW"
    assert result.success_count == 3
    assert result.failure_count == 0


def test_two_failures_outweighing_success_are_high_risk():
    session = _session()
    _case(session, "a", "VERIFIED_FAILED")
    _case(session, "b", "EXECUTION_FAILED")
    _case(session, "c", "VERIFIED_SUCCESS")
    incident, action = _action(session, "current")

    result = assess(session, action=action, incident=incident)

    assert result.level == "HIGH"
    assert result.failure_count == 2


def test_other_action_ids_do_not_contaminate_risk():
    session = _session()
    _case(session, "a", "VERIFIED_FAILED", regression=True, action_id="resync_ntp")
    incident, action = _action(session, "current")

    result = assess(session, action=action, incident=incident)

    assert result.level == "INSUFFICIENT_EVIDENCE"
    assert result.sample_count == 0


def test_other_clusters_are_informational_not_blocking():
    session = _session()
    _case(session, "a", "VERIFIED_SUCCESS", regression=True, cluster_id="cluster-lab")
    incident, action = _action(session, "current", cluster_id="cluster-production")

    result = assess(session, action=action, incident=incident)

    assert result.level == "INSUFFICIENT_EVIDENCE"
    assert result.evidence["global_regression_count"] == 1
    assert result.regression_count == 0


def test_new_evidence_invalidates_acknowledged_fingerprint():
    session = _session()
    incident, action = _action(session, "current")
    from shared.change_risk import acknowledge

    acknowledged = acknowledge(session, action=action, incident=incident)
    session.commit()
    row = session.query(ChangeRiskAssessment).filter_by(action_id=action.id).one()
    assert row.acknowledged_hash == acknowledged.fingerprint

    _case(session, "a", "VERIFIED_FAILED")
    refreshed = assess_and_record(session, action=action, incident=incident)
    session.commit()

    assert refreshed.fingerprint != acknowledged.fingerprint
    assert row.acknowledged_hash != row.assessment_hash
