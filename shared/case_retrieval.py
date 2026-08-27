"""Conservative verified-Case retrieval for diagnosis context, never authorization."""
from __future__ import annotations

import json
import re

from shared.models import Action, RemediationCase


_BAD_VERDICTS = {"FALSE_POSITIVE", "UNSAFE", "INEFFECTIVE"}


def _major(version: str | None) -> str:
    match = re.search(r"\d+", version or "")
    return match.group(0) if match else "unknown"


def _load(raw: str | None, fallback):
    try:
        value = json.loads(raw or "null")
    except (TypeError, ValueError):
        return fallback
    return fallback if value is None else value


def _normalized_nodes(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({node.strip() for node in value if isinstance(node, str) and node.strip()}))


def _eligible(case: RemediationCase) -> bool:
    if case.outcome != "VERIFIED_SUCCESS" or not case.preflight_snapshot_json:
        return False
    try:
        pre_state = _load(case.pre_state_json, {})
    except (TypeError, ValueError):
        pre_state = {}
    if isinstance(pre_state, dict) and pre_state.get("synthetic_injection") is True:
        return False
    if case.prompt_version == "legacy-backfill-v1" or case.operator_verdict in _BAD_VERDICTS:
        return False
    if any(value is True for value in (case.regressed_1h, case.regressed_24h, case.regressed_7d)):
        return False
    snapshot = _load(case.preflight_snapshot_json, {})
    contract = snapshot.get("registry") if isinstance(snapshot, dict) else None
    return bool(
        isinstance(contract, dict)
        and contract.get("action_id")
        and str(contract.get("version")) == str(case.playbook_version)
    )


def find_verified_cases(
    session, *, incident_id: str, cluster_id: str | None, fault_family: str,
    nodes: list[str] | None, ceph_version: str | None, deployment_mode: str | None,
    limit: int = 3,
) -> list[dict]:
    """Exact scope/entity retrieval. Semantic ranking is a later, secondary layer."""
    incoming_nodes = _normalized_nodes(nodes)
    query = (
        session.query(RemediationCase, Action)
        .join(Action, Action.id == RemediationCase.action_id)
        .filter(RemediationCase.incident_id != incident_id)
        .filter(RemediationCase.fault_family == fault_family)
        .filter(RemediationCase.outcome == "VERIFIED_SUCCESS")
    )
    if cluster_id is None:
        query = query.filter(RemediationCase.cluster_id.is_(None))
    else:
        query = query.filter(RemediationCase.cluster_id == cluster_id)
    candidates = query.order_by(RemediationCase.verified_at.desc()).all()
    references: list[dict] = []
    for case, action in candidates:
        if not _eligible(case):
            continue
        if _major(case.ceph_version) != _major(ceph_version):
            continue
        if (case.deployment_mode or "unknown") != (deployment_mode or "unknown"):
            continue
        entities = _load(case.entity_keys_json, {})
        if _normalized_nodes(entities.get("nodes") if isinstance(entities, dict) else None) != incoming_nodes:
            continue
        references.append({
            "case_id": case.id,
            "playbook_id": action.action_id,
            "playbook_version": case.playbook_version,
            "diagnosis": (case.diagnosis or "")[:500],
            "verified_at": case.verified_at.isoformat() if case.verified_at else None,
            "recovery_seconds": case.recovery_seconds,
            "operator_verdict": case.operator_verdict,
            "regressed_1h": case.regressed_1h,
            "regressed_24h": case.regressed_24h,
            "regressed_7d": case.regressed_7d,
        })
        if len(references) >= max(1, limit):
            break
    return references
