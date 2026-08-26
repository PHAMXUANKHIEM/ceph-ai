"""Fail-closed supervised learning projections for Loki daemon findings."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime

from shared.models import (
    Action,
    LogFaultStat,
    LogFinding,
    LogIngestRun,
    LogLearningAudit,
    LogLearningSample,
    LogPattern,
    RemediationCase,
)

PARSER_VERSION = "log-pattern-v1"
SEMANTIC_VERSION = "daemon-fault-v1"
BAD_OPERATOR_VERDICTS = {"FALSE_POSITIVE", "UNSAFE", "INEFFECTIVE"}
VALID_OPERATOR_VERDICTS = {
    "CORRECT", "FALSE_POSITIVE", "WRONG_ROOT_CAUSE", "INEFFECTIVE", "UNSAFE",
}


def _json_list(value: str | None) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in decoded if isinstance(item, str)] if isinstance(decoded, list) else []


def _identity(finding: LogFinding, patterns: list[LogPattern]) -> tuple[str, str | None, str | None, str]:
    daemons = _json_list(finding.affected_daemons_json)
    entities = _json_list(finding.semantic_entities_json)
    hosts = _json_list(finding.affected_hosts_json)
    daemon_type = patterns[0].daemon_type if patterns else (daemons[0].split(".", 1)[0] if daemons else "unknown")
    daemon_entities = [item for item in entities if item.startswith("daemon:")]
    daemon_entity = next(
        (
            item for item in daemon_entities
            if item.removeprefix("daemon:").split(".", 1)[0] == daemon_type
        ),
        None,
    )
    if daemon_entity is None:
        daemon_entity = next(
            (f"daemon:{item}" for item in daemons if item.split(".", 1)[0] == daemon_type),
            None,
        )
    daemon_id = daemon_entity.removeprefix("daemon:") if daemon_entity else None
    host_entity = next((item for item in entities if item.startswith("host:")), None)
    host = host_entity.removeprefix("host:") if host_entity else (hosts[0] if hosts else None)
    entity_key = daemon_entity or host_entity or next(
        (item for item in entities if item != "unknown"), "unknown"
    )
    return daemon_type or "unknown", daemon_id, host, entity_key


def _fingerprint(finding: LogFinding, run: LogIngestRun, pattern_ids: list[str]) -> str:
    payload = {
        "cluster_id": finding.cluster_id,
        "fault_family": finding.fault_family or "unknown",
        "patterns": sorted(pattern_ids),
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "parser_version": PARSER_VERSION,
        "semantic_version": SEMANTIC_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record_finding_sample(session, finding: LogFinding, *, now: datetime | None = None) -> LogLearningSample:
    """Idempotently snapshot a finding. Never grants trust or executes actions."""
    existing = session.query(LogLearningSample).filter_by(log_finding_id=finding.id).one_or_none()
    if existing is not None:
        return existing
    run = session.get(LogIngestRun, finding.ingest_run_id)
    if run is None:
        raise ValueError(f"missing ingest run {finding.ingest_run_id}")
    pattern_ids = sorted(set(_json_list(finding.evidence_pattern_ids_json)))
    patterns = (
        session.query(LogPattern).filter(LogPattern.id.in_(pattern_ids)).order_by(LogPattern.id).all()
        if pattern_ids else []
    )
    daemon_type, daemon_id, host, entity_key = _identity(finding, patterns)
    state = "CORRELATED" if finding.correlated_incident_id else "CANDIDATE"
    exclusion = "awaiting verified remediation outcome" if finding.correlated_incident_id else "no correlated incident"
    if run.status != "OK":
        state, exclusion = "INSUFFICIENT_EVIDENCE", f"ingest coverage is {run.status}"
    sample = LogLearningSample(
        cluster_id=finding.cluster_id,
        log_finding_id=finding.id,
        ingest_run_id=run.id,
        incident_id=finding.correlated_incident_id,
        daemon_type=daemon_type,
        daemon_id=daemon_id,
        host=host,
        fault_family=finding.fault_family or "unknown",
        entity_key=entity_key,
        pattern_ids_json=json.dumps(pattern_ids, separators=(",", ":")),
        evidence_fingerprint=_fingerprint(finding, run, pattern_ids),
        source=run.source,
        window_start=run.window_start,
        window_end=run.window_end,
        ingest_status=run.status,
        parser_version=PARSER_VERSION,
        semantic_version=SEMANTIC_VERSION,
        prompt_version=finding.prompt_version,
        model_name=finding.model_name,
        diagnosis_confidence=finding.confidence,
        recommended_playbook_id=finding.recommended_action_id,
        state=state,
        label="UNVERIFIED",
        eligible_for_learning=False,
        exclusion_reason=exclusion,
        created_at=now or datetime.utcnow(),
        updated_at=now or datetime.utcnow(),
    )
    session.add(sample)
    session.flush()
    return sample


def evaluate_sample(session, sample: LogLearningSample, *, now: datetime | None = None) -> bool:
    """Project verified Case Memory truth into a sample, failing closed."""
    before = (
        sample.incident_id, sample.remediation_case_id, sample.action_id, sample.state,
        sample.label, sample.eligible_for_learning, sample.exclusion_reason,
        sample.outcome_source, sample.verified_at, sample.regressed,
        sample.recommended_playbook_id, sample.playbook_version,
    )
    run = session.get(LogIngestRun, sample.ingest_run_id)
    if run is None or run.status != "OK":
        sample.state = "INSUFFICIENT_EVIDENCE"
        sample.label = "UNVERIFIED"
        sample.eligible_for_learning = False
        sample.exclusion_reason = "missing ingest provenance" if run is None else f"ingest coverage is {run.status}"
    elif sample.operator_verdict in BAD_OPERATOR_VERDICTS | {"WRONG_ROOT_CAUSE"}:
        sample.state = "FALSE_POSITIVE"
        sample.label = "VERIFIED_FAILED"
        sample.eligible_for_learning = True
        sample.exclusion_reason = None
        sample.outcome_source = "OPERATOR_VERDICT"
        sample.verified_at = sample.operator_verdict_at or (now or datetime.utcnow())
    else:
        finding = session.get(LogFinding, sample.log_finding_id)
        sample.incident_id = finding.correlated_incident_id if finding else sample.incident_id
        case = None
        action = None
        if sample.incident_id:
            case = (
                session.query(RemediationCase)
                .filter_by(incident_id=sample.incident_id)
                .order_by(RemediationCase.created_at.desc())
                .first()
            )
        if case:
            action = session.get(Action, case.action_id)
            sample.remediation_case_id = case.id
            sample.action_id = case.action_id
            sample.recommended_playbook_id = action.action_id if action else sample.recommended_playbook_id
            sample.playbook_version = case.playbook_version
            sample.regressed = any(value is True for value in (case.regressed_1h, case.regressed_24h, case.regressed_7d))
            bad_verdict = case.operator_verdict in BAD_OPERATOR_VERDICTS
            if bad_verdict or sample.regressed:
                sample.state = "REGRESSED" if sample.regressed else "FALSE_POSITIVE"
                sample.label = "VERIFIED_FAILED"
                sample.eligible_for_learning = True
                sample.exclusion_reason = None
                sample.outcome_source = "OPERATOR_VERDICT" if bad_verdict else "RECURRENCE_EVALUATOR"
                sample.verified_at = case.verified_at or (now or datetime.utcnow())
            elif case.outcome == "VERIFIED_SUCCESS":
                sample.state = "VERIFIED_SUCCESS"
                sample.label = "VERIFIED_SUCCESS"
                sample.eligible_for_learning = True
                sample.exclusion_reason = None
                sample.outcome_source = "TELEMETRY_POST_CHECK"
                sample.verified_at = case.verified_at
            elif case.outcome == "VERIFIED_FAILED":
                sample.state = "VERIFIED_FAILED"
                sample.label = "VERIFIED_FAILED"
                sample.eligible_for_learning = True
                sample.exclusion_reason = None
                sample.outcome_source = "TELEMETRY_POST_CHECK"
                sample.verified_at = case.verified_at
            else:
                sample.state = "DIAGNOSED"
                sample.label = "UNVERIFIED"
                sample.eligible_for_learning = False
                sample.exclusion_reason = f"remediation outcome is {case.outcome}"
        elif sample.incident_id:
            sample.state = "CORRELATED"
            sample.label = "UNVERIFIED"
            sample.eligible_for_learning = False
            sample.exclusion_reason = "awaiting Remediation Case"
        else:
            sample.state = "CANDIDATE"
            sample.label = "UNVERIFIED"
            sample.eligible_for_learning = False
            sample.exclusion_reason = "no correlated incident"
    sample.updated_at = now or datetime.utcnow()
    after = (
        sample.incident_id, sample.remediation_case_id, sample.action_id, sample.state,
        sample.label, sample.eligible_for_learning, sample.exclusion_reason,
        sample.outcome_source, sample.verified_at, sample.regressed,
        sample.recommended_playbook_id, sample.playbook_version,
    )
    return before != after


def set_operator_verdict(
    session, *, sample: LogLearningSample, verdict: str, note: str,
    actor: str, now: datetime | None = None,
) -> None:
    """Set an audited supervised label; positive labels still need telemetry."""
    verdict = (verdict or "").strip().upper()
    note = (note or "").strip()
    if verdict not in VALID_OPERATOR_VERDICTS:
        raise ValueError("invalid operator verdict")
    if verdict != "CORRECT" and len(note) < 5:
        raise ValueError("a reason of at least 5 characters is required")
    if len(note) > 2000:
        raise ValueError("operator note is too long")
    now = now or datetime.utcnow()
    previous = {
        "verdict": sample.operator_verdict,
        "note": sample.operator_note,
        "actor": sample.operator_verdict_by,
        "at": sample.operator_verdict_at.isoformat() if sample.operator_verdict_at else None,
    }
    sample.operator_verdict = verdict
    sample.operator_note = note or None
    sample.operator_verdict_by = actor
    sample.operator_verdict_at = now
    evaluate_sample(session, sample, now=now)
    session.add(LogLearningAudit(
        sample_id=sample.id,
        event_type="OPERATOR_VERDICT_UPDATED",
        actor=actor,
        previous_value_json=json.dumps(previous, sort_keys=True),
        new_value_json=json.dumps({"verdict": verdict, "note": note or None}, sort_keys=True),
        created_at=now,
    ))


def reclassify_unverified_samples(session, *, now: datetime | None = None, limit: int = 500) -> int:
    """Apply the current server catalogue to unverified legacy snapshots."""
    from watcher.log_semantics import derive_identity

    now = now or datetime.utcnow()
    samples = (
        session.query(LogLearningSample)
        .filter(LogLearningSample.eligible_for_learning.is_(False))
        .order_by(LogLearningSample.updated_at)
        .limit(limit)
        .all()
    )
    changed = 0
    for sample in samples:
        finding = session.get(LogFinding, sample.log_finding_id)
        if finding is None:
            continue
        pattern_ids = _json_list(finding.evidence_pattern_ids_json)
        patterns = session.query(LogPattern).filter(LogPattern.id.in_(pattern_ids)).all() if pattern_ids else []
        semantic = derive_identity(
            [pattern.template for pattern in patterns],
            _json_list(finding.affected_hosts_json),
            _json_list(finding.affected_daemons_json),
        )
        new_family = semantic.fault_family or "unknown"
        if new_family == sample.fault_family and set(semantic.entities) == set(_json_list(finding.semantic_entities_json)):
            continue
        previous = {"fault_family": sample.fault_family, "entity_key": sample.entity_key}
        finding.fault_family = semantic.fault_family
        finding.semantic_entities_json = json.dumps(semantic.entities)
        sample.fault_family = new_family
        daemon_type, daemon_id, host, entity_key = _identity(finding, patterns)
        sample.daemon_type = daemon_type
        sample.daemon_id = daemon_id
        sample.host = host
        sample.entity_key = entity_key
        run = session.get(LogIngestRun, sample.ingest_run_id)
        if run is not None:
            sample.evidence_fingerprint = _fingerprint(finding, run, pattern_ids)
        sample.semantic_version = SEMANTIC_VERSION
        sample.updated_at = now
        session.add(LogLearningAudit(
            sample_id=sample.id, event_type="SEMANTIC_RECLASSIFIED", actor="system",
            previous_value_json=json.dumps(previous, sort_keys=True),
            new_value_json=json.dumps({"fault_family": new_family, "entity_key": entity_key}, sort_keys=True),
            created_at=now,
        ))
        changed += 1
    session.commit()
    return changed


def correlate_unverified_samples(session, *, now: datetime | None = None, limit: int = 500) -> int:
    """Retry deterministic Incident correlation after delayed Loki analysis.

    Loki findings can arrive after autonomous remediation has already closed
    the matching Incident.  Correlation at finding creation is therefore not
    sufficient; retry unlinked samples using their own ingest window as the
    event clock, never wall-clock time.
    """
    from watcher.incident_correlation import correlate_finding

    changed = 0
    samples = (
        session.query(LogLearningSample)
        .filter(LogLearningSample.eligible_for_learning.is_(False))
        .filter(LogLearningSample.incident_id.is_(None))
        .order_by(LogLearningSample.updated_at)
        .limit(limit)
        .all()
    )
    for sample in samples:
        finding = session.get(LogFinding, sample.log_finding_id)
        run = session.get(LogIngestRun, sample.ingest_run_id)
        if finding is None or run is None or run.status != "OK":
            continue
        incident = correlate_finding(session, finding, now=run.window_end)
        if incident is None:
            continue
        sample.incident_id = incident.id
        sample.state = "CORRELATED"
        sample.exclusion_reason = "awaiting Remediation Case"
        sample.updated_at = now or datetime.utcnow()
        changed += 1
    session.commit()
    return changed


def reconcile_samples(session, *, now: datetime | None = None, limit: int = 500) -> int:
    """Create missing samples and refresh outcome projections in bounded batches."""
    now = now or datetime.utcnow()
    existing_ids = {row[0] for row in session.query(LogLearningSample.log_finding_id).all()}
    findings = (
        session.query(LogFinding).filter(~LogFinding.id.in_(existing_ids)).order_by(LogFinding.created_at).limit(limit).all()
        if existing_ids else session.query(LogFinding).order_by(LogFinding.created_at).limit(limit).all()
    )
    changed = 0
    for finding in findings:
        record_finding_sample(session, finding, now=now)
        changed += 1
    changed += reclassify_unverified_samples(session, now=now, limit=limit)
    changed += correlate_unverified_samples(session, now=now, limit=limit)
    for sample in session.query(LogLearningSample).order_by(LogLearningSample.updated_at).limit(limit).all():
        changed += int(evaluate_sample(session, sample, now=now))
    session.commit()
    return changed


def recompute_fault_stats(session, *, now: datetime | None = None) -> int:
    """Idempotently replace audit-only aggregates; never proposes promotion."""
    from shared.trust_engine import wilson_lower_bound

    now = now or datetime.utcnow()
    grouped: dict[tuple[str, str, str, str, str], list[LogLearningSample]] = defaultdict(list)
    for sample in session.query(LogLearningSample).all():
        key = (
            sample.cluster_id, sample.daemon_type, sample.fault_family,
            sample.recommended_playbook_id or "observation_only",
            sample.playbook_version or "none",
        )
        grouped[key].append(sample)
    changed = 0
    active_keys = set(grouped)
    for stat in session.query(LogFaultStat).all():
        key = (stat.cluster_id, stat.daemon_type, stat.fault_family, stat.playbook_id, stat.playbook_version)
        if key not in active_keys:
            session.delete(stat)
            changed += 1
    for key, samples in grouped.items():
        eligible = [sample for sample in samples if sample.eligible_for_learning]
        successes = sum(sample.label == "VERIFIED_SUCCESS" for sample in eligible)
        failures = sum(sample.label == "VERIFIED_FAILED" for sample in eligible)
        values = {
            "sample_count": len(samples), "verified_count": len(eligible),
            "success_count": successes, "failure_count": failures,
            "inconclusive_count": sum(sample.state == "INCONCLUSIVE" for sample in samples),
            "trust_score": wilson_lower_bound(successes, successes + failures),
            "promotion_candidate_at": None,
            "promotion_blocked_reason": "log learning is audit-only; shadow commissioning not enabled",
            "updated_at": now,
        }
        stat = session.query(LogFaultStat).filter_by(
            cluster_id=key[0], daemon_type=key[1], fault_family=key[2],
            playbook_id=key[3], playbook_version=key[4],
        ).one_or_none()
        if stat is None:
            stat = LogFaultStat(
                cluster_id=key[0], daemon_type=key[1], fault_family=key[2],
                playbook_id=key[3], playbook_version=key[4], **values,
            )
            session.add(stat)
            changed += 1
        else:
            comparable = {name: getattr(stat, name) for name in values if name != "updated_at"}
            target = {name: value for name, value in values.items() if name != "updated_at"}
            if comparable != target:
                for name, value in values.items():
                    setattr(stat, name, value)
                changed += 1
    session.commit()
    return changed
