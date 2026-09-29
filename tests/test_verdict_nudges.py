from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import verdict_nudges
from shared.db import Base
from shared.models import Action, Incident, RemediationCase

NOW = datetime(2026, 9, 28, 12, 0)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _case(session, index, *, family="OSD_DOWN", action_id="investigate_manually", status="REJECTED",
          outcome="PROPOSED", confidence=None, verdict=None, age_days=1):
    created = NOW - timedelta(days=age_days)
    incident = Incident(ceph_code=f"CODE_{index}", status="RESOLVED", detected_at=created, created_at=created)
    session.add(incident)
    session.flush()
    action = Action(incident_id=incident.id, action_id=action_id, classification="RISKY", status=status,
                    created_at=created)
    session.add(action)
    session.flush()
    case = RemediationCase(
        incident_id=incident.id, action_id=action.id, fault_family=family, evidence_fingerprint="f" * 64,
        prompt_version="v1", classification="RISKY", autonomy_decision="APPROVAL_REQUIRED",
        outcome=outcome, diagnosis_confidence=confidence, operator_verdict=verdict, created_at=created,
    )
    session.add(case)
    session.flush()
    return case


def test_ranking_prefers_known_outcomes_disagreement_and_concrete_actions():
    session = _session()
    plain = _case(session, 1)
    verified = _case(session, 2, family="MON_DOWN", outcome="VERIFIED_FAILED")
    disagree = _case(session, 3, family="SLOW_OPS", action_id="restart_osd_daemon", confidence=0.9)
    # All three have no weak label; lift the per-weak-label cap so this test
    # is only about the ranking (the cap has its own test below).
    chosen = verdict_nudges.select_cases(session, limit=3, per_label_cap=3, now=NOW)
    # Disagreement + concrete action and a known outcome score the same (5);
    # both must beat the plain placeholder case, their mutual order is by id.
    assert {item.case_id for item in chosen[:2]} == {disagree.id, verified.id}
    assert chosen[2].case_id == plain.id
    by_id = {item.case_id: item for item in chosen}
    assert "AI tự tin nhưng bị từ chối" in by_id[disagree.id].reasons
    assert "outcome VERIFIED_FAILED" in by_id[verified.id].reasons


def test_labelled_cases_old_cases_and_per_family_cap_are_respected():
    session = _session()
    for index in range(4):
        _case(session, index, family="NODE_UNREACHABLE")
    _case(session, 10, family="OSD_DOWN", verdict="CORRECT")
    _case(session, 11, family="MON_DOWN", age_days=30)
    chosen = verdict_nudges.select_cases(session, limit=10, per_family_cap=2, now=NOW)
    assert [item.fault_family for item in chosen] == ["NODE_UNREACHABLE", "NODE_UNREACHABLE"]


def test_rare_families_rank_above_well_labelled_ones():
    session = _session()
    for index in range(3):
        _case(session, 100 + index, family="OSD_DOWN", verdict="CORRECT")
    common = _case(session, 1, family="OSD_DOWN")
    rare = _case(session, 2, family="PG_DAMAGED")
    chosen = verdict_nudges.select_cases(session, limit=2, now=NOW)
    assert [item.case_id for item in chosen] == [rare.id, common.id]


def test_a_nudged_case_is_not_nudged_again_and_the_day_is_detected():
    session = _session()
    first = _case(session, 1)
    second = _case(session, 2, family="MON_DOWN")
    batch = verdict_nudges.select_cases(session, limit=1, now=NOW)
    verdict_nudges.mark_nudged(session, batch[0])
    session.flush()
    remaining = verdict_nudges.select_cases(session, limit=5, now=NOW)
    assert [item.case_id for item in remaining] == [({first.id, second.id} - {batch[0].case_id}).pop()]
    assert verdict_nudges.nudged_since(session, datetime(2000, 1, 1))
    assert verdict_nudges.select_cases(session, limit=0, now=NOW) == []


def test_batch_is_capped_per_weak_label():
    session = _session()
    for index in range(4):
        _case(session, index, family=f"FAMILY_{index}")
    chosen = verdict_nudges.select_cases(session, limit=4, per_label_cap=2, now=NOW)
    assert len(chosen) == 2 and {item.weak_label for item in chosen} == {None}
