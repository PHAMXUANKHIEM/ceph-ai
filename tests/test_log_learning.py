import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.log_learning import (
    correlate_unverified_samples,
    evaluate_sample,
    record_finding_sample,
    recompute_fault_stats,
    set_operator_verdict,
)
from shared.models import (
    Action,
    Cluster,
    Incident,
    LogFaultStat,
    LogFinding,
    LogIngestRun,
    LogLearningSample,
    LogLearningAudit,
    LogPattern,
)
from shared.remediation_cases import create_for_action, record_verified


def _session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(session, *, ingest_status="OK", correlated=True):
    now = datetime(2026, 8, 25, 1, 0)
    cluster = Cluster(
        name="lab", ceph_mon_nodes="10.0.0.1", ssh_user="root",
        ssh_key_path="/tmp/key",
    )
    session.add(cluster); session.flush()
    run = LogIngestRun(
        cluster_id=cluster.id, source="loki", window_start=now - timedelta(hours=1),
        window_end=now, status=ingest_status,
    )
    incident = Incident(
        cluster_id=cluster.id, ceph_code="OSD_DOWN", status="DIAGNOSING",
        detected_at=now - timedelta(minutes=5),
    )
    session.add_all([run, incident]); session.flush()
    pattern = LogPattern(
        cluster_id=cluster.id, fingerprint="a" * 40,
        template="osd.<N> heartbeat no reply", daemon_type="osd", severity=-1,
        first_seen_at=now - timedelta(minutes=10), last_seen_at=now,
    )
    session.add(pattern); session.flush()
    finding = LogFinding(
        cluster_id=cluster.id, ingest_run_id=run.id, verdict="FINDING",
        severity="WARNING", confidence="HIGH", title="OSD heartbeat",
        evidence_pattern_ids_json=json.dumps([pattern.id]),
        affected_hosts_json=json.dumps(["node-a"]),
        affected_daemons_json=json.dumps(["osd"]),
        fault_family="network_heartbeat",
        semantic_entities_json=json.dumps(["daemon:osd.5", "host:node-a"]),
        correlated_incident_id=incident.id if correlated else None,
        recommended_action_id="restart_osd_daemon", dedupe_key="d" * 64,
        status="OPEN", model_name="test-model", prompt_version="log-intel-v1",
    )
    session.add(finding); session.flush()
    return now, cluster, incident, finding


def test_finding_snapshot_is_idempotent_and_contains_no_raw_log():
    session = _session()
    _now, _cluster, _incident, finding = _seed(session)

    first = record_finding_sample(session, finding)
    second = record_finding_sample(session, finding)
    session.commit()

    assert first.id == second.id
    assert session.query(LogLearningSample).count() == 1
    assert first.state == "CORRELATED"
    assert first.daemon_type == "osd"
    assert first.daemon_id == "osd.5"
    assert first.entity_key == "daemon:osd.5"
    assert len(first.evidence_fingerprint) == 64
    assert "heartbeat no reply" not in first.pattern_ids_json
    assert first.eligible_for_learning is False


def test_partial_loki_coverage_is_never_eligible():
    session = _session()
    _now, _cluster, _incident, finding = _seed(session, ingest_status="PARTIAL")
    sample = record_finding_sample(session, finding)

    evaluate_sample(session, sample)

    assert sample.state == "INSUFFICIENT_EVIDENCE"
    assert sample.eligible_for_learning is False
    assert sample.exclusion_reason == "ingest coverage is PARTIAL"


def test_verified_case_becomes_positive_sample_and_updates_wilson_stat():
    session = _session()
    now, cluster, incident, finding = _seed(session)
    action = Action(
        incident_id=incident.id, action_id="restart_osd_daemon",
        classification="SAFE", status="AUTO_EXECUTED", target_nodes='["node-a"]',
    )
    session.add(action); session.flush()
    case = create_for_action(
        session, incident=incident, action=action,
        redacted_envelope={
            "nodes": ["node-a"], "ceph_exec_mode": "cephadm",
            "cluster_snapshot": {"ceph_version": "18.2.4"},
        },
        diagnosis="heartbeat failure", model_provider="test",
    )
    record_verified(
        session, incident_id=incident.id, succeeded=True,
        verified_at=now + timedelta(minutes=10), post_state={"health": "OK"},
    )
    sample = record_finding_sample(session, finding)

    assert evaluate_sample(session, sample, now=now + timedelta(minutes=11)) is True
    assert sample.remediation_case_id == case.id
    assert sample.action_id == action.id
    assert sample.label == "VERIFIED_SUCCESS"
    assert sample.eligible_for_learning is True
    assert sample.outcome_source == "TELEMETRY_POST_CHECK"

    assert recompute_fault_stats(session, now=now + timedelta(minutes=12)) == 1
    stat = session.query(LogFaultStat).one()
    assert stat.cluster_id == cluster.id
    assert stat.verified_count == 1
    assert stat.success_count == 1
    assert 0 < stat.trust_score < 1
    assert stat.promotion_candidate_at is None
    assert "audit-only" in stat.promotion_blocked_reason


def test_delayed_loki_sample_correlates_to_resolved_verified_incident():
    session = _session()
    now, _cluster, incident, finding = _seed(session, correlated=False)
    incident.status = "RESOLVED"
    action = Action(
        incident_id=incident.id, action_id="restart_osd_daemon",
        classification="SAFE", status="AUTO_EXECUTED", target_nodes='["node-a"]',
    )
    session.add(action); session.flush()
    create_for_action(
        session, incident=incident, action=action,
        redacted_envelope={"nodes": ["node-a"], "cluster_snapshot": {}},
        diagnosis="heartbeat failure", model_provider="test",
    )
    record_verified(
        session, incident_id=incident.id, succeeded=True,
        verified_at=now + timedelta(minutes=10), post_state={"health": "OK"},
    )
    sample = record_finding_sample(session, finding, now=now)

    assert correlate_unverified_samples(session, now=now + timedelta(minutes=11)) == 1
    assert evaluate_sample(session, sample, now=now + timedelta(minutes=11)) is True
    assert sample.incident_id == incident.id
    assert sample.label == "VERIFIED_SUCCESS"
    assert sample.eligible_for_learning is True


def test_bad_operator_verdict_is_a_verified_negative_even_after_success():
    session = _session()
    now, _cluster, incident, finding = _seed(session)
    action = Action(
        incident_id=incident.id, action_id="restart_osd_daemon",
        classification="SAFE", status="AUTO_EXECUTED", target_nodes='["node-a"]',
    )
    session.add(action); session.flush()
    case = create_for_action(
        session, incident=incident, action=action,
        redacted_envelope={"nodes": ["node-a"], "cluster_snapshot": {}},
        diagnosis="wrong target", model_provider="test",
    )
    record_verified(
        session, incident_id=incident.id, succeeded=True, verified_at=now,
        post_state={"health": "OK"},
    )
    case.operator_verdict = "UNSAFE"
    sample = record_finding_sample(session, finding)

    evaluate_sample(session, sample, now=now)

    assert sample.state == "FALSE_POSITIVE"
    assert sample.label == "VERIFIED_FAILED"
    assert sample.eligible_for_learning is True
    assert sample.outcome_source == "OPERATOR_VERDICT"


def test_sample_operator_verdict_is_audited_and_fails_closed_for_positive():
    session = _session()
    now, _cluster, _incident, finding = _seed(session, correlated=False)
    sample = record_finding_sample(session, finding, now=now)

    set_operator_verdict(
        session, sample=sample, verdict="CORRECT", note="", actor="admin", now=now,
    )
    assert sample.eligible_for_learning is False
    assert sample.label == "UNVERIFIED"

    set_operator_verdict(
        session, sample=sample, verdict="FALSE_POSITIVE",
        note="Log kiểm thử có chủ đích", actor="admin", now=now,
    )
    session.commit()
    assert sample.eligible_for_learning is True
    assert sample.label == "VERIFIED_FAILED"
    assert sample.outcome_source == "OPERATOR_VERDICT"
    assert session.query(LogLearningAudit).count() == 2


def test_bad_verdict_requires_a_meaningful_note():
    session = _session()
    now, _cluster, _incident, finding = _seed(session)
    sample = record_finding_sample(session, finding, now=now)
    try:
        set_operator_verdict(
            session, sample=sample, verdict="UNSAFE", note="no", actor="admin", now=now,
        )
    except ValueError as exc:
        assert "at least 5" in str(exc)
    else:
        raise AssertionError("short unsafe verdict note was accepted")
