"""Deterministic, redacted Remediation Case Memory write helpers."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from shared.models import Action, ActionStatus, Cluster, Incident, IncidentStatus, RemediationCase


_SENSITIVE_KEY = re.compile(
    r"password|secret|token|api.?key|keyring|credential|ssh_key", re.IGNORECASE,
)
_CASE_JSON_FIELDS = (
    "entity_keys_json", "topology_snapshot_json", "preflight_snapshot_json",
    "pre_state_json", "post_state_json", "rollback_state_json", "side_effects_json",
)


def _scrub(value):
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _json(value) -> str | None:
    return json.dumps(_scrub(value), ensure_ascii=False, sort_keys=True) if value is not None else None


def _ceph_version(snapshot: dict) -> str | None:
    for key in ("ceph_version", "version"):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _safe_load(raw: str | None, fallback):
    try:
        value = json.loads(raw or "null")
    except (TypeError, ValueError):
        return fallback
    return fallback if value is None else value


def _fingerprint(*, fault_family: str, entities: dict, snapshot: dict,
                 deployment_mode: str | None, ceph_version: str | None) -> str:
    source = {
        "fault_family": fault_family,
        "entities": _scrub(entities),
        "health_codes": sorted((snapshot.get("checks") or {}).keys()),
        "deployment_mode": deployment_mode,
        "ceph_version": ceph_version,
    }
    return hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def create_for_action(
    session, *, incident: Incident, action: Action, redacted_envelope: dict,
    diagnosis: str | None, model_provider: str | None,
) -> RemediationCase:
    """Create once in the Action transaction; caller owns commit."""
    existing = session.query(RemediationCase).filter_by(action_id=action.id).one_or_none()
    if existing is not None:
        return existing
    snapshot = redacted_envelope.get("cluster_snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snapshot = _scrub(snapshot)
    entities = _scrub({
        "nodes": redacted_envelope.get("nodes") if isinstance(redacted_envelope.get("nodes"), list) else [],
        "action_params": _safe_load(action.action_params, None),
    })
    fingerprint = _fingerprint(
        fault_family=incident.ceph_code, entities=entities, snapshot=snapshot,
        deployment_mode=redacted_envelope.get("ceph_exec_mode"),
        ceph_version=_ceph_version(snapshot),
    )
    decision = "AUTO_EXECUTE" if action.classification in {"READ_ONLY", "SAFE"} else "PENDING_APPROVAL"
    row = RemediationCase(
        incident_id=incident.id, action_id=action.id, cluster_id=incident.cluster_id,
        fault_family=incident.ceph_code, entity_keys_json=_json(entities),
        evidence_fingerprint=fingerprint, ceph_version=_ceph_version(snapshot),
        deployment_mode=redacted_envelope.get("ceph_exec_mode"),
        topology_snapshot_json=_json(snapshot.get("osdmap") or snapshot.get("monmap")),
        diagnosis=diagnosis, prompt_version="incident-diagnosis-v1", model_provider=model_provider,
        classification=action.classification, autonomy_decision=decision,
        playbook_version="v1", pre_state_json=_json(snapshot), outcome="PROPOSED",
    )
    session.add(row)
    return row


def record_execution(session, *, action_id: str, succeeded: bool, executed_at: datetime | None) -> None:
    row = session.query(RemediationCase).filter_by(action_id=action_id).one_or_none()
    if row is None:
        return
    row.executed_at = executed_at
    row.started_at = row.started_at or executed_at
    row.outcome = "EXECUTED_PENDING_VERIFY" if succeeded else "EXECUTION_FAILED"


def record_verified(
    session, *, incident_id: str, succeeded: bool, verified_at: datetime, post_state: dict | None,
) -> None:
    rows = session.query(RemediationCase).filter_by(incident_id=incident_id).all()
    for row in rows:
        if row.outcome not in {"EXECUTED_PENDING_VERIFY", "PROPOSED"}:
            continue
        row.outcome = "VERIFIED_SUCCESS" if succeeded else "VERIFIED_FAILED"
        row.verified_at = verified_at
        row.post_state_json = _json(post_state or {})
        if succeeded:
            incident = session.get(Incident, incident_id)
            if incident is not None:
                row.recovery_seconds = max(0, int((verified_at - incident.detected_at).total_seconds()))


def record_inconclusive(session, *, action_id: str, at: datetime, reason: str) -> None:
    row = session.query(RemediationCase).filter_by(action_id=action_id).one_or_none()
    if row is None:
        return
    row.outcome = "INCONCLUSIVE"
    row.verified_at = at
    row.side_effects_json = _json({"reason": reason, "auto_retry": False})


def _legacy_outcome(action: Action, incident: Incident) -> str:
    if action.status == ActionStatus.INCONCLUSIVE.value:
        return "INCONCLUSIVE"
    if action.status == ActionStatus.FAILED.value:
        return "EXECUTION_FAILED"
    if action.status == ActionStatus.REJECTED.value:
        return "REJECTED"
    if action.status == ActionStatus.EXECUTING.value:
        return "EXECUTING"
    if action.status in {ActionStatus.AUTO_EXECUTED.value, ActionStatus.EXECUTED.value}:
        if incident.status == IncidentStatus.VERIFYING.value:
            return "EXECUTED_PENDING_VERIFY"
        if incident.status == IncidentStatus.RESOLVED.value:
            # Old RESOLVED rows may only mean SSH exit 0. Never feed these
            # into trust as verified successes without fresh telemetry.
            return "LEGACY_RESOLVED_UNVERIFIED"
        return "EXECUTED_UNVERIFIED"
    return "PROPOSED"


def backfill_missing_cases(session, *, limit: int = 200) -> int:
    """Cover legacy/non-AI Actions conservatively in bounded batches."""
    missing = (
        session.query(Action)
        .filter(~session.query(RemediationCase).filter(RemediationCase.action_id == Action.id).exists())
        .order_by(Action.created_at, Action.id)
        .limit(max(1, limit))
        .all()
    )
    created = 0
    for action in missing:
        incident = session.get(Incident, action.incident_id)
        if incident is None:
            continue
        cluster = session.get(Cluster, incident.cluster_id) if incident.cluster_id else None
        evidence = _safe_load(incident.signal_evidence_json, {})
        evidence = evidence if isinstance(evidence, dict) else {"signal": evidence}
        envelope = {
            "nodes": _safe_load(action.target_nodes, []),
            "ceph_exec_mode": cluster.ceph_exec_mode if cluster else None,
            "cluster_snapshot": evidence,
        }
        row = create_for_action(
            session, incident=incident, action=action, redacted_envelope=envelope,
            diagnosis=incident.diagnosis_text, model_provider=None,
        )
        row.prompt_version = "legacy-backfill-v1"
        row.outcome = _legacy_outcome(action, incident)
        row.executed_at = action.executed_at
        row.started_at = action.executed_at
        row.side_effects_json = _json({
            "backfilled": True,
            "trust_eligible": False,
            "reason": "historical telemetry provenance is incomplete",
        })
        created += 1
    session.commit()
    return created


_REGRESSION_WINDOWS = (
    ("regressed_1h", timedelta(hours=1)),
    ("regressed_24h", timedelta(hours=24)),
    ("regressed_7d", timedelta(days=7)),
)


def evaluate_regressions(session, *, now: datetime, limit: int = 200) -> int:
    """Evaluate recurrence without prematurely writing a negative result.

    Identity is exact cluster + fault family + normalized entity JSON. A
    recurrence may set True immediately; False is only final after the full
    window closes. Historical/unverified cases are never candidates.
    """
    candidates = (
        session.query(RemediationCase)
        .filter(RemediationCase.outcome == "VERIFIED_SUCCESS")
        .filter(RemediationCase.verified_at.isnot(None))
        .filter(
            (RemediationCase.regressed_1h.is_(None))
            | (RemediationCase.regressed_24h.is_(None))
            | (RemediationCase.regressed_7d.is_(None))
        )
        .order_by(RemediationCase.verified_at, RemediationCase.id)
        .limit(max(1, limit))
        .all()
    )
    changed = 0
    for case in candidates:
        case_changed = False
        recurrence = (
            session.query(Incident.detected_at)
            .join(RemediationCase, RemediationCase.incident_id == Incident.id)
            .filter(RemediationCase.id != case.id)
            .filter(RemediationCase.fault_family == case.fault_family)
            .filter(RemediationCase.entity_keys_json == case.entity_keys_json)
            .filter(Incident.detected_at > case.verified_at)
        )
        if case.cluster_id is None:
            recurrence = recurrence.filter(RemediationCase.cluster_id.is_(None))
        else:
            recurrence = recurrence.filter(RemediationCase.cluster_id == case.cluster_id)
        recurrence_at = recurrence.order_by(Incident.detected_at).limit(1).scalar()

        for field, window in _REGRESSION_WINDOWS:
            if getattr(case, field) is not None:
                continue
            window_end = case.verified_at + window
            if recurrence_at is not None and recurrence_at <= window_end:
                setattr(case, field, True)
                case_changed = True
            elif now >= window_end:
                setattr(case, field, False)
                case_changed = True
        if case_changed:
            changed += 1
    session.commit()
    return changed


def scrub_existing_case_memory(session) -> int:
    """Remove sensitive JSON values already persisted and repair fingerprints."""
    changed = 0
    for case in session.query(RemediationCase).all():
        case_changed = False
        decoded = {}
        for field in _CASE_JSON_FIELDS:
            raw = getattr(case, field)
            if raw is None:
                decoded[field] = None
                continue
            value = _safe_load(raw, None)
            scrubbed = _scrub(value)
            encoded = _json(scrubbed)
            decoded[field] = scrubbed
            if encoded != raw:
                setattr(case, field, encoded)
                case_changed = True
        if case_changed:
            entities = decoded.get("entity_keys_json")
            snapshot = decoded.get("pre_state_json")
            case.evidence_fingerprint = _fingerprint(
                fault_family=case.fault_family,
                entities=entities if isinstance(entities, dict) else {},
                snapshot=snapshot if isinstance(snapshot, dict) else {},
                deployment_mode=case.deployment_mode,
                ceph_version=case.ceph_version,
            )
            changed += 1
    session.commit()
    return changed
