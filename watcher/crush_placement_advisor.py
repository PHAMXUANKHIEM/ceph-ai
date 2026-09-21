"""Read-only CRUSH placement advisor.

The advisor consumes the already published CRUSH snapshot.  It can identify
weight-versus-usage skew and rule/domain mismatches, but it does not claim
actual PG placement or data movement without PG acting/up evidence.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

MAX_FINDINGS = 200
SKEW_THRESHOLD = 0.5
DOMAIN_STEP_NAMES = {"chooseleaf_firstn", "chooseleaf_ind", "choose_firstn", "choose_ind"}


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    value = node.get("children")
    return [child for child in value if isinstance(child, dict)] if isinstance(value, list) else []


def _walk(node: dict[str, Any], types: Counter[str]) -> None:
    node_type = str(node.get("type") or "").strip()
    if node_type:
        types[node_type] += 1
    for child in _children(node):
        _walk(child, types)


def _sibling_groups(node: dict[str, Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    children = _children(node)
    groups = [(node, children)] if len(children) >= 2 else []
    for child in children:
        groups.extend(_sibling_groups(child))
    return groups


def _rule_domain(rule: dict[str, Any]) -> str | None:
    steps = rule.get("steps")
    if not isinstance(steps, list):
        return None
    for step in steps:
        if not isinstance(step, dict):
            continue
        op = str(step.get("op") or "").strip()
        if op in DOMAIN_STEP_NAMES and step.get("type"):
            return str(step["type"])
    return None


def _finding(
    *,
    code: str,
    severity: str,
    title: str,
    reason: str,
    target: dict[str, Any],
    evidence: dict[str, Any],
    recommendation: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "reason": reason,
        "target": target,
        "evidence": evidence,
        "confidence": "high" if severity == "high" else "medium",
        "recommendation": recommendation,
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
    }


def build_crush_placement_advisor(
    *,
    cluster_id: str,
    cluster_name: str,
    crush_snapshot: dict[str, Any] | None,
    skew_threshold: float = SKEW_THRESHOLD,
) -> dict[str, Any]:
    """Build bounded CRUSH findings without producing a mutation command."""
    snapshot = crush_snapshot if isinstance(crush_snapshot, dict) else {}
    roots = snapshot.get("roots")
    roots = [root for root in roots if isinstance(root, dict)] if isinstance(roots, list) else []
    rules = snapshot.get("rules")
    rules = [rule for rule in rules if isinstance(rule, dict)] if isinstance(rules, list) else []
    threshold = max(0.1, float(skew_threshold))
    findings: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, str]] = []
    types: Counter[str] = Counter()

    for root in roots:
        _walk(root, types)

    for root in roots:
        for parent, children in _sibling_groups(root):
            weighted = []
            for child in children:
                weight = _number(child.get("weight_normalized", child.get("weight")))
                used = _number(child.get("bytes_used"))
                if weight is None or used is None:
                    continue
                weighted.append((child, weight, used))
            total_weight = sum(item[1] for item in weighted)
            total_used = sum(item[2] for item in weighted)
            if len(weighted) < 2 or total_weight <= 0 or total_used <= 0:
                continue
            for child, weight, used in weighted:
                expected = weight / total_weight
                actual = used / total_used
                deviation = (actual - expected) / expected if expected else None
                if deviation is None or abs(deviation) < threshold:
                    continue
                child_name = str(child.get("name") or child.get("id") or "unknown")
                parent_name = str(parent.get("name") or parent.get("id") or "unknown")
                findings.append(_finding(
                    code="CRUSH_WEIGHT_USAGE_SKEW",
                    severity="medium",
                    title="Phân bố sử dụng lệch so với CRUSH weight",
                    reason=(
                        f"{child_name} trong {parent_name} đang chiếm "
                        f"{actual:.1%} usage nhưng weight kỳ vọng {expected:.1%}."
                    ),
                    target={
                        "type": str(child.get("type") or "crush_node"),
                        "name": child_name,
                    },
                    evidence={
                        "parent": parent_name,
                        "actual_share": round(actual, 4),
                        "expected_share": round(expected, 4),
                        "relative_deviation": round(deviation, 4),
                        "threshold": threshold,
                    },
                    recommendation=(
                        "Kiểm tra CRUSH weight, rebalance đang chạy và PG acting "
                        "trước khi reweight."
                    ),
                ))

    observed_types = set(types)
    rule_analysis = []
    for rule in rules:
        domain = _rule_domain(rule)
        rule_name = str(rule.get("rule_name") or rule.get("name") or rule.get("rule_id") or "unknown")
        rule_analysis.append({
            "rule": rule_name,
            "failure_domain": domain,
            "domain_observed": bool(domain and domain in observed_types),
        })
        if domain and domain not in observed_types:
            findings.append(_finding(
                code="CRUSH_RULE_DOMAIN_MISMATCH",
                severity="high",
                title="CRUSH rule yêu cầu failure domain không tồn tại trong snapshot",
                reason=f"Rule {rule_name} yêu cầu domain {domain}, nhưng snapshot không có node type này.",
                target={"type": "crush_rule", "name": rule_name},
                evidence={
                    "requested_failure_domain": domain,
                    "observed_node_types": sorted(observed_types),
                },
                recommendation="Kiểm tra CRUSH hierarchy và rule trước khi áp dụng thay đổi.",
            ))

    if not roots:
        evidence_gaps.append({
            "code": "CRUSH_SNAPSHOT_UNAVAILABLE",
            "reason": "Chưa có CRUSH snapshot hợp lệ.",
        })
    evidence_gaps.append({
        "code": "PG_ACTING_EVIDENCE_UNAVAILABLE",
        "reason": "CRUSH snapshot chỉ có topology; chưa có PG acting/up mapping để mô phỏng data movement.",
    })
    if not rules:
        evidence_gaps.append({
            "code": "CRUSH_RULE_EVIDENCE_UNAVAILABLE",
            "reason": "Snapshot chưa có CRUSH rule để kiểm tra failure-domain.",
        })
    evidence_gaps.extend([
        {
            "code": "POOL_RULE_BINDING_UNAVAILABLE",
            "reason": "Chưa có mapping pool-to-rule trong input advisor.",
        },
        {
            "code": "FAILURE_DOMAIN_SIMULATION_UNAVAILABLE",
            "reason": "Chưa có PG placement và failure-domain loss model; kết quả movement/risk để unknown.",
        },
    ])

    findings.sort(key=lambda item: (
        {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(item["severity"], 9),
        str(item["target"].get("name", "")),
    ))
    findings = findings[:MAX_FINDINGS]
    return {
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "status": "observed" if roots else "not_available",
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
        "assumptions": {
            "skew_threshold": threshold,
            "data_movement_estimate": "unknown",
            "failure_domain_loss_risk": "unknown",
        },
        "summary": {
            "root_count": len(roots),
            "node_type_counts": dict(sorted(types.items())),
            "rule_count": len(rules),
            "finding_count": len(findings),
        },
        "rule_analysis": rule_analysis,
        "findings": findings,
        "evidence_gaps": evidence_gaps,
        "limits": {
            "max_findings": MAX_FINDINGS,
            "mutation_commands_included": False,
        },
    }
