from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.models import Action, Incident, PlaybookStat
from shared.remediation_cases import create_for_action
from shared.trust_engine import recompute_playbook_stats, wilson_lower_bound


def _session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _case(session, *, outcome="VERIFIED_SUCCESS", version="18.2.4", mode="cephadm"):
    incident = Incident(ceph_code="MON_CLOCK_SKEW", status="RESOLVED", detected_at=datetime.utcnow())
    session.add(incident); session.flush()
    action = Action(
        incident_id=incident.id, action_id="resync_ntp", classification="SAFE",
        status="AUTO_EXECUTED", target_nodes='["mon-a"]',
    )
    session.add(action); session.flush()
    case = create_for_action(
        session, incident=incident, action=action,
        redacted_envelope={
            "nodes": ["mon-a"], "ceph_exec_mode": mode,
            "cluster_snapshot": {"ceph_version": version},
        },
        diagnosis="clock skew", model_provider="test",
    )
    case.outcome = outcome
    case.verified_at = datetime.utcnow() if outcome.startswith("VERIFIED_") else None
    session.commit()
    return case


def test_wilson_lower_bound_is_conservative_for_small_samples():
    assert wilson_lower_bound(0, 0) == 0
    assert 0 < wilson_lower_bound(1, 1) < 0.3
    assert wilson_lower_bound(98, 100) > wilson_lower_bound(9, 10)


def test_recompute_counts_verified_outcomes_and_is_idempotent():
    session = _session()
    for _ in range(9):
        _case(session)
    _case(session, outcome="VERIFIED_FAILED")
    _case(session, outcome="PROPOSED")

    assert recompute_playbook_stats(session) == 1
    stat = session.query(PlaybookStat).one()
    assert (stat.proposed_count, stat.executed_count, stat.verified_count) == (11, 10, 10)
    assert (stat.success_count, stat.failure_count, stat.inconclusive_count) == (9, 1, 0)
    assert stat.trust_score == wilson_lower_bound(9, 10)
    assert stat.maturity_level == "L2"
    assert stat.promotion_candidate_at is None
    assert recompute_playbook_stats(session) == 0


def test_scope_separates_ceph_major_and_deployment_mode():
    session = _session()
    _case(session, version="18.2.4", mode="cephadm")
    _case(session, version="17.2.7", mode="cephadm")
    _case(session, version="18.2.4", mode="none")
    recompute_playbook_stats(session)
    assert {row.scope_key for row in session.query(PlaybookStat)} == {
        "ceph_major=18|deployment=cephadm",
        "ceph_major=17|deployment=cephadm",
        "ceph_major=18|deployment=none",
    }


def test_legacy_unverified_and_corrupt_contract_never_increase_trust():
    session = _session()
    legacy = _case(session)
    legacy.prompt_version = "legacy-backfill-v1"
    corrupt = _case(session)
    corrupt.preflight_snapshot_json = "not-json"
    unverified = _case(session, outcome="LEGACY_RESOLVED_UNVERIFIED")
    unverified.preflight_snapshot_json = None
    session.commit()

    assert recompute_playbook_stats(session) == 0
    assert session.query(PlaybookStat).count() == 0


def test_regression_and_bad_operator_verdict_count_as_failure():
    session = _session()
    regressed = _case(session); regressed.regressed_1h = True
    unsafe = _case(session); unsafe.operator_verdict = "UNSAFE"
    session.commit()
    recompute_playbook_stats(session)
    stat = session.query(PlaybookStat).one()
    assert (stat.success_count, stat.failure_count) == (0, 2)
    assert stat.trust_score == 0


def test_scope_is_zeroed_if_its_cases_later_become_ineligible():
    session = _session()
    case = _case(session)
    recompute_playbook_stats(session)
    stat = session.query(PlaybookStat).one()
    assert stat.verified_count == 1

    case.preflight_snapshot_json = "corrupt"
    session.commit()
    assert recompute_playbook_stats(session) == 1
    assert stat.verified_count == 0
    assert stat.trust_score == 0
    assert stat.maturity_level == "L0"
    assert "no eligible" in stat.auto_disabled_reason
