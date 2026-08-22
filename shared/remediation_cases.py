"""Deterministic, redacted Remediation Case Memory write helpers."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from shared.models import Action, Incident, RemediationCase


def _json(value) -> str | None:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else None


def _ceph_version(snapshot: dict) -> str | None:
    for key in ("ceph_version", "version"):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


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
    entities = {
        "nodes": redacted_envelope.get("nodes") if isinstance(redacted_envelope.get("nodes"), list) else [],
        "action_params": json.loads(action.action_params) if action.action_params else None,
    }
    fingerprint_source = {
        "fault_family": incident.ceph_code,
        "entities": entities,
        "health_codes": sorted((snapshot.get("checks") or {}).keys()),
        "deployment_mode": redacted_envelope.get("ceph_exec_mode"),
        "ceph_version": _ceph_version(snapshot),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
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
