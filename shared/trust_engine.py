"""Deterministic Playbook Trust aggregates from verified Case Memory only."""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime

from shared.models import Action, PlaybookStat, RemediationCase


_EXECUTED_OUTCOMES = {
    "EXECUTED_PENDING_VERIFY", "VERIFIED_SUCCESS", "VERIFIED_FAILED",
    "EXECUTION_FAILED", "INCONCLUSIVE",
}
_BAD_OPERATOR_VERDICTS = {"FALSE_POSITIVE", "UNSAFE", "INEFFECTIVE"}


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.96) -> float:
    if total <= 0 or successes < 0 or successes > total:
        return 0.0
    p = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    centre = p + z2 / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    return max(0.0, min(1.0, (centre - margin) / denominator))


def _ceph_major(version: str | None) -> str:
    match = re.search(r"\d+", version or "")
    return match.group(0) if match else "unknown"


def scope_key(case: RemediationCase) -> str:
    return (
        f"ceph_major={_ceph_major(case.ceph_version)}|"
        f"deployment={case.deployment_mode or 'unknown'}"
    )


def _trust_eligible(case: RemediationCase) -> bool:
    # Pha-1 backfill and pre-registry Cases have incomplete provenance. They
    # remain visible in Case Memory but can never grant trust.
    if not case.preflight_snapshot_json or case.prompt_version == "legacy-backfill-v1":
        return False
    try:
        snapshot = json.loads(case.preflight_snapshot_json)
    except (TypeError, ValueError):
        return False
    registry = snapshot.get("registry") if isinstance(snapshot, dict) else None
    return bool(
        isinstance(registry, dict)
        and registry.get("action_id")
        and str(registry.get("version")) == str(case.playbook_version)
    )


def _verified_result(case: RemediationCase) -> str | None:
    if case.outcome not in {"VERIFIED_SUCCESS", "VERIFIED_FAILED"}:
        return None
    if case.outcome == "VERIFIED_FAILED":
        return "failure"
    if case.operator_verdict in _BAD_OPERATOR_VERDICTS:
        return "failure"
    if any(value is True for value in (case.regressed_1h, case.regressed_24h, case.regressed_7d)):
        return "failure"
    return "success"


def recompute_playbook_stats(session, *, now: datetime | None = None) -> int:
    """Idempotently replace aggregates; never promotes autonomy by itself."""
    now = now or datetime.utcnow()
    grouped: dict[tuple[str, str, str], list[RemediationCase]] = defaultdict(list)
    rows = (
        session.query(RemediationCase)
        .join(Action, Action.id == RemediationCase.action_id)
        .all()
    )
    action_ids = {action_id: playbook_id for action_id, playbook_id in session.query(Action.id, Action.action_id)}
    for case in rows:
        if not _trust_eligible(case):
            continue
        playbook_id = action_ids.get(case.action_id)
        if not playbook_id:
            continue
        grouped[(playbook_id, case.playbook_version, scope_key(case))].append(case)

    changed = 0
    active_keys = set(grouped)
    for (playbook_id, version, scope), cases in grouped.items():
        stat = session.query(PlaybookStat).filter_by(
            playbook_id=playbook_id, playbook_version=version, scope_key=scope,
        ).one_or_none()
        if stat is None:
            stat = PlaybookStat(
                playbook_id=playbook_id, playbook_version=version, scope_key=scope,
            )
            session.add(stat)
        results = [_verified_result(case) for case in cases]
        successes = sum(result == "success" for result in results)
        failures = sum(result == "failure" for result in results)
        verified = successes + failures
        values = {
            "proposed_count": len(cases),
            "executed_count": sum(case.outcome in _EXECUTED_OUTCOMES for case in cases),
            "verified_count": verified,
            "success_count": successes,
            "failure_count": failures,
            "inconclusive_count": sum(case.outcome == "INCONCLUSIVE" for case in cases),
            "trust_score": wilson_lower_bound(successes, verified),
            "maturity_level": "L0" if not cases else "L1" if verified == 0 else "L2",
            "last_failure_at": max(
                (case.verified_at for case, result in zip(cases, results) if result == "failure" and case.verified_at),
                default=None,
            ),
            # Trust Engine reports evidence only. Promotion remains an admin
            # workflow introduced later in Pha 3.
            "promotion_candidate_at": None,
            "auto_disabled_reason": None,
        }
        if any(getattr(stat, key) != value for key, value in values.items()):
            for key, value in values.items():
                setattr(stat, key, value)
            changed += 1
    for stat in session.query(PlaybookStat).all():
        key = (stat.playbook_id, stat.playbook_version, stat.scope_key)
        if key in active_keys:
            continue
        empty_values = {
            "proposed_count": 0, "executed_count": 0, "verified_count": 0,
            "success_count": 0, "failure_count": 0, "inconclusive_count": 0,
            "trust_score": 0.0, "maturity_level": "L0", "last_failure_at": None,
            "promotion_candidate_at": None,
            "auto_disabled_reason": "no eligible verified Case Memory in this scope",
        }
        if any(getattr(stat, key) != value for key, value in empty_values.items()):
            for field, value in empty_values.items():
                setattr(stat, field, value)
            changed += 1
    session.commit()
    return changed
