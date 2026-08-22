"""Deterministic Playbook Trust aggregates from verified Case Memory only."""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta

from shared.models import Action, PlaybookStat, RemediationCase


_EXECUTED_OUTCOMES = {
    "EXECUTED_PENDING_VERIFY", "VERIFIED_SUCCESS", "VERIFIED_FAILED",
    "EXECUTION_FAILED", "INCONCLUSIVE",
}
_BAD_OPERATOR_VERDICTS = {"FALSE_POSITIVE", "UNSAFE", "INEFFECTIVE"}
SHADOW_MIN_VERIFIED_SAMPLES = 20
SHADOW_MIN_TRUST_SCORE = 0.85


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
        f"cluster={case.cluster_id or 'legacy'}|"
        f"ceph_major={_ceph_major(case.ceph_version)}|"
        f"deployment={case.deployment_mode or 'unknown'}"
    )


def record_shadow_decision(
    session, *, case: RemediationCase, action: Action, now: datetime | None = None,
) -> str:
    """Freeze a hypothetical decision; this function cannot execute or promote."""
    now = now or datetime.utcnow()
    stat = session.query(PlaybookStat).filter_by(
        playbook_id=action.action_id,
        playbook_version=case.playbook_version,
        scope_key=scope_key(case),
    ).one_or_none()
    samples = stat.verified_count if stat else 0
    score = stat.trust_score if stat else 0.0
    if action.classification not in {"READ_ONLY", "SAFE"}:
        decision, reason = "HOLD", "classification is not SAFE/READ_ONLY"
    elif samples < SHADOW_MIN_VERIFIED_SAMPLES:
        decision, reason = "HOLD", f"verified samples {samples}/{SHADOW_MIN_VERIFIED_SAMPLES}"
    elif score < SHADOW_MIN_TRUST_SCORE:
        decision, reason = "HOLD", f"Wilson trust {score:.3f} below {SHADOW_MIN_TRUST_SCORE:.2f}"
    elif stat and stat.auto_disabled_reason:
        decision, reason = "HOLD", f"trust scope disabled: {stat.auto_disabled_reason}"
    else:
        decision, reason = "WOULD_EXECUTE", "shadow trust and classification gates passed"
    case.shadow_decision = decision
    case.shadow_reason = reason
    case.shadow_trust_score = score
    case.shadow_sample_count = samples
    case.shadow_recorded_at = now
    return decision


def shadow_comparison(case: RemediationCase) -> str:
    """Compare frozen shadow intent with later verified/operator truth."""
    bad = case.operator_verdict in _BAD_OPERATOR_VERDICTS
    failed = case.outcome == "VERIFIED_FAILED" or bad or any(
        value is True for value in (case.regressed_1h, case.regressed_24h, case.regressed_7d)
    )
    successful = case.outcome == "VERIFIED_SUCCESS" and not failed
    if not successful and not failed:
        return "PENDING_OUTCOME"
    if case.shadow_decision == "WOULD_EXECUTE":
        return "MATCH_SUCCESS" if successful else "UNSAFE_MISS"
    return "MISSED_OPPORTUNITY" if successful else "CORRECT_HOLD"


def shadow_evaluation_report(
    session, *, now: datetime | None = None, window_days: int = 28,
) -> dict:
    """Deterministic read-only commissioning evidence for the Shadow window."""
    now = now or datetime.utcnow()
    since = now - timedelta(days=max(1, window_days))
    rows = (
        session.query(RemediationCase, Action)
        .join(Action, Action.id == RemediationCase.action_id)
        .filter(RemediationCase.shadow_recorded_at.isnot(None))
        .filter(RemediationCase.shadow_recorded_at >= since)
        .order_by(RemediationCase.shadow_recorded_at, RemediationCase.id)
        .all()
    )

    def summarize(items: list[tuple[RemediationCase, Action]]) -> dict:
        comparisons = [shadow_comparison(case) for case, _action in items]
        evaluated = sum(value != "PENDING_OUTCOME" for value in comparisons)
        would_execute_evaluated = sum(
            case.shadow_decision == "WOULD_EXECUTE" and comparison != "PENDING_OUTCOME"
            for (case, _action), comparison in zip(items, comparisons)
        )
        match_success = comparisons.count("MATCH_SUCCESS")
        unsafe_miss = comparisons.count("UNSAFE_MISS")
        precision = (
            match_success / would_execute_evaluated if would_execute_evaluated else None
        )
        return {
            "total": len(items), "evaluated": evaluated,
            "pending": comparisons.count("PENDING_OUTCOME"),
            "would_execute": sum(case.shadow_decision == "WOULD_EXECUTE" for case, _ in items),
            "match_success": match_success, "unsafe_miss": unsafe_miss,
            "correct_hold": comparisons.count("CORRECT_HOLD"),
            "missed_opportunity": comparisons.count("MISSED_OPPORTUNITY"),
            "precision": precision,
        }

    overall = summarize(rows)
    first_at = rows[0][0].shadow_recorded_at if rows else None
    observed_days = min(window_days, max(0, (now - first_at).days)) if first_at else 0
    # Reporting only: readiness never unlocks or enables Autopilot.
    overall.update({
        "window_days": window_days, "observed_days": observed_days,
        "ready_for_review": bool(
            observed_days >= 14 and overall["evaluated"] >= 20
            and overall["unsafe_miss"] == 0
            and overall["precision"] is not None and overall["precision"] >= 0.95
        ),
    })
    grouped: dict[str, list[tuple[RemediationCase, Action]]] = defaultdict(list)
    for row in rows:
        grouped[row[1].action_id].append(row)
    overall["playbooks"] = [
        {"playbook_id": playbook_id, **summarize(items)}
        for playbook_id, items in sorted(grouped.items())
    ]
    return overall


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
        unsafe_verdict = any(case.operator_verdict == "UNSAFE" for case in cases)
        poor_quality = verified >= 4 and successes / verified < 0.80
        disabled_reason = (
            "operator marked a case unsafe" if unsafe_verdict else
            "verified success rate below 80%" if poor_quality else None
        )
        maturity = (
            "L0" if not cases else "L1" if verified == 0 else
            "L1" if disabled_reason else
            "L3" if stat.maturity_level == "L3" else "L2"
        )
        values = {
            "proposed_count": len(cases),
            "executed_count": sum(case.outcome in _EXECUTED_OUTCOMES for case in cases),
            "verified_count": verified,
            "success_count": successes,
            "failure_count": failures,
            "inconclusive_count": sum(case.outcome == "INCONCLUSIVE" for case in cases),
            "trust_score": wilson_lower_bound(successes, verified),
            "maturity_level": maturity,
            "last_failure_at": max(
                (case.verified_at for case, result in zip(cases, results) if result == "failure" and case.verified_at),
                default=None,
            ),
            "auto_disabled_reason": disabled_reason,
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
            "promotion_blocked_reason": "no eligible verified Case Memory in this scope",
            "auto_disabled_reason": "no eligible verified Case Memory in this scope",
        }
        if any(getattr(stat, key) != value for key, value in empty_values.items()):
            for field, value in empty_values.items():
                setattr(stat, field, value)
            changed += 1
    session.commit()
    return changed


def evaluate_promotion_candidates(session, *, now: datetime | None = None) -> int:
    """Propose L2→L3 review only; never changes maturity or execution policy."""
    now = now or datetime.utcnow()
    since = now - timedelta(days=30)
    actions = {row.id: row for row in session.query(Action).all()}
    changed = 0
    for stat in session.query(PlaybookStat).all():
        if stat.maturity_level == "L3":
            blocked = "already promoted to L3"
            if stat.promotion_candidate_at is not None or stat.promotion_blocked_reason != blocked:
                stat.promotion_candidate_at = None
                stat.promotion_blocked_reason = blocked
                changed += 1
            continue
        cases = [
            case for case in session.query(RemediationCase).filter(
                RemediationCase.playbook_version == stat.playbook_version,
                RemediationCase.verified_at >= since,
            ).all()
            if actions.get(case.action_id)
            and actions[case.action_id].action_id == stat.playbook_id
            and scope_key(case) == stat.scope_key
            and _trust_eligible(case)
            and _verified_result(case) is not None
        ]
        cases.sort(key=lambda case: (case.verified_at, case.id))
        successes = sum(_verified_result(case) == "success" for case in cases)
        false_positives = sum(case.operator_verdict == "FALSE_POSITIVE" for case in cases)
        rollbacks = sum(bool(case.rollback_state_json) for case in cases)
        consecutive = 0
        for case in reversed(cases):
            if _verified_result(case) != "success":
                break
            consecutive += 1
        confidence_errors = [
            abs(float(case.diagnosis_confidence) - (1.0 if _verified_result(case) == "success" else 0.0))
            for case in cases if case.diagnosis_confidence is not None
        ]
        calibration = sum(confidence_errors) / len(confidence_errors) if confidence_errors else None
        contract = _load_contract(cases[-1]) if cases else {}
        reasons = []
        if len(cases) < 10: reasons.append(f"verified cases {len(cases)}/10")
        if not cases or successes / len(cases) < 0.98: reasons.append("30d success rate below 98%")
        if cases and false_positives / len(cases) > 0.01: reasons.append("false-positive rate above 1%")
        if cases and rollbacks / len(cases) > 0.02: reasons.append("rollback rate above 2%")
        if consecutive < 5: reasons.append(f"consecutive successes {consecutive}/5")
        if calibration is None: reasons.append("confidence calibration unavailable")
        elif calibration > 0.10: reasons.append("confidence calibration error above 0.10")
        if contract.get("target_schema") in {None, "manual"} or not contract.get("max_targets"):
            reasons.append("target is not deterministic")
        if not contract.get("preflight"): reasons.append("preflight unavailable")
        if not contract.get("postcheck"): reasons.append("post-check unavailable")
        if contract.get("max_autonomy") not in {"L3", "L4", "L5"}:
            reasons.append("playbook autonomy ceiling below L3")
        if any(case.operator_verdict == "UNSAFE" for case in cases):
            reasons.append("recent severe/unsafe verdict exists")
        blocked = "; ".join(reasons) or None
        candidate_at = stat.promotion_candidate_at
        if not reasons and candidate_at is None:
            candidate_at = now
        if reasons:
            candidate_at = None
        if stat.promotion_candidate_at != candidate_at or stat.promotion_blocked_reason != blocked:
            stat.promotion_candidate_at = candidate_at
            stat.promotion_blocked_reason = blocked
            changed += 1
    session.commit()
    return changed


def _load_contract(case: RemediationCase) -> dict:
    try:
        snapshot = json.loads(case.preflight_snapshot_json or "null")
    except (TypeError, ValueError):
        return {}
    registry = snapshot.get("registry") if isinstance(snapshot, dict) else None
    return registry if isinstance(registry, dict) else {}
