from datetime import datetime, timedelta

import pytest

from shared import shadow_policy as sp

GOOD = {"affected_nodes": 1, "diagnosis_confidence": 0.95, "triage_confidence": 0.8,
        "shadow_sample_count": 25, "shadow_trust_score": 0.9}
CONTRACT = sp.ContractInfo(True, max_targets=1, rollback="undo_it")


def test_execute_only_when_every_gate_passes():
    decision = sp.evaluate(GOOD, "resync_ntp", "llm", CONTRACT, frr=0.0, frr_budget=0.05)
    assert decision.recommendation == sp.EXECUTE and decision.reasons == []


@pytest.mark.parametrize("change,contract,frr,reason", [
    ({}, sp.ContractInfo(False), None, "chưa có contract"),
    ({"affected_nodes": 3}, CONTRACT, None, "blast radius 3 node > giới hạn 1"),
    ({}, sp.ContractInfo(True, 1, None), None, "không có rollback"),
    ({"diagnosis_confidence": 0.85}, CONTRACT, None, "độ tin 0.85 < 0.9 (llm)"),
    ({"diagnosis_confidence": None}, CONTRACT, None, "độ tin — < 0.9"),
    ({"shadow_sample_count": 5}, CONTRACT, None, "Trust Engine 5/20 mẫu"),
    ({"shadow_trust_score": 0.6}, CONTRACT, None, "trust 0.60 < 0.85"),
    ({}, CONTRACT, 0.2, "FRR 30 ngày 20.0% > ngân sách 5%"),
])
def test_any_failing_gate_escalates_with_its_reason(change, contract, frr, reason):
    decision = sp.evaluate({**GOOD, **change}, "resync_ntp", "llm", contract, frr=frr, frr_budget=0.05)
    assert decision.recommendation == sp.ESCALATE
    assert any(reason in item for item in decision.reasons), decision.reasons


def test_rules_use_their_own_confidence_and_placeholder_always_escalates():
    assert sp.evaluate({**GOOD, "diagnosis_confidence": None}, "resync_ntp", "rules", CONTRACT, None, 0.05).recommendation == sp.EXECUTE
    assert sp.evaluate({**GOOD, "triage_confidence": 0.6}, "resync_ntp", "rules", CONTRACT, None, 0.05).recommendation == sp.ESCALATE
    assert sp.evaluate(GOOD, "investigate_manually", "llm", CONTRACT, None, 0.05).recommendation == sp.ESCALATE


def test_contract_info_reads_the_real_registry():
    unknown = sp.contract_info("definitely_not_a_playbook")
    assert unknown == sp.ContractInfo(False)


def test_frr_needs_enough_labels_and_counts_only_execute_recommendations():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from shared.db import Base
    from shared.models import Action, AutonomyDecision, Incident, RemediationCase

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    now = datetime(2026, 9, 28, 12, 0)

    def add(index, verdict, recommendation="execute", age=1):
        incident = Incident(ceph_code=f"MON_CLOCK_SKEW:m{index}", status="RESOLVED", cluster_id="c1", detected_at=now)
        session.add(incident)
        session.flush()
        action = Action(incident_id=incident.id, action_id="resync_ntp", classification="SAFE", status="EXECUTED")
        session.add(action)
        session.flush()
        case = RemediationCase(incident_id=incident.id, action_id=action.id, fault_family="MON_CLOCK_SKEW",
                               evidence_fingerprint=str(index).ljust(64, "x"), prompt_version="v1",
                               classification="SAFE", autonomy_decision="AUTO_EXECUTE", operator_verdict=verdict)
        session.add(case)
        session.flush()
        session.add(AutonomyDecision(case_id=case.id, incident_id=incident.id, cluster_id="c1",
                                     fault_family="MON_CLOCK_SKEW", context_json="{}", candidates_json="[]",
                                     chosen_action="resync_ntp", chosen_by="llm", propensity=1.0, policy_version="v",
                                     shadow_recommendation=recommendation, created_at=now - timedelta(days=age)))

    for index in range(9):
        add(index, "CORRECT")
    add(9, "UNSAFE")
    session.flush()
    assert sp.false_release_rate(session, "c1", now) == pytest.approx(0.1)
    add(10, "UNSAFE", recommendation="escalate")        # not this policy's release
    add(11, "UNSAFE", age=40)                           # outside the window
    add(12, "INCONCLUSIVE")                             # no judgement
    session.flush()
    assert sp.false_release_rate(session, "c1", now) == pytest.approx(0.1)
    assert sp.false_release_rate(session, "other", now) is None
