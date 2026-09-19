"""Read-only replication/EC policy and failure-domain evaluation."""
from __future__ import annotations

from collections.abc import Mapping


def _int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rows(payload: object, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, Mapping):
        for key in keys:
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _crush_rule(payload: object, rule_id: object) -> dict:
    wanted = _int(rule_id)
    for row in _rows(payload, "rules"):
        if wanted is not None and _int(row.get("rule_id")) == wanted:
            return row
        if wanted is None and str(row.get("rule_name") or row.get("name")) == str(rule_id):
            return row
    return {}


def _failure_domain(rule: Mapping[str, object]) -> str | None:
    steps = rule.get("steps") or rule.get("rules") or []
    if not isinstance(steps, list):
        return None
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        operation = str(step.get("op") or step.get("operation") or "").lower()
        if "choose" in operation:
            return str(step.get("type") or step.get("type_name") or "") or None
    return None


def _domain_count(tree_payload: object, domain_type: str | None) -> int:
    if not domain_type:
        return 0
    nodes = tree_payload.get("nodes") if isinstance(tree_payload, Mapping) else None
    if not isinstance(nodes, list):
        return 0
    names = {
        str(row.get("name")) for row in nodes
        if isinstance(row, Mapping)
        and str(row.get("type") or "").lower() == domain_type.lower()
        and row.get("name")
    }
    return len(names)


def build_durability_policy(
    overview: Mapping[str, object],
    crush_payload: object,
    osd_tree_payload: object,
    *,
    ec_profile: Mapping[str, object] | None = None,
) -> dict:
    """Return policy posture and domain sufficiency without proposing changes."""
    pool_type = str(overview.get("type") or "").lower()
    rule_id = overview.get("crush_rule")
    rule = _crush_rule(crush_payload, rule_id)
    domain_type = _failure_domain(rule)
    domains = _domain_count(osd_tree_payload, domain_type)
    notes: list[str] = []
    gaps: list[str] = []
    requirements = 0
    policy = {}

    if pool_type in {"replicated", "replica", "replication"}:
        size = _int(overview.get("replica_size"))
        min_size = _int(overview.get("min_size"))
        requirements = size or 0
        policy = {"mode": "replicated", "size": size, "min_size": min_size}
        if size is None or size < 1:
            gaps.append("thiếu replica_size")
        if min_size is None or min_size < 1:
            gaps.append("thiếu min_size")
        if size is not None and min_size is not None and min_size > size:
            notes.append("min_size lớn hơn size; policy không hợp lệ.")
        if size is not None and size < 2:
            notes.append("Replica size < 2 không có redundancy đầy đủ.")
    elif pool_type in {"erasure", "erasure-coded", "erasure_coded", "ec"}:
        profile = ec_profile or {}
        k = _int(profile.get("k"))
        m = _int(profile.get("m"))
        requirements = (k or 0) + (m or 0)
        policy = {
            "mode": "erasure_coded", "profile": overview.get("erasure_code_profile"),
            "k": k, "m": m, "chunks": requirements,
        }
        if k is None or m is None or k < 1 or m < 1:
            gaps.append("EC profile thiếu k/m hoặc profile chưa đọc được")
    else:
        gaps.append("không xác định được replication/EC mode")

    if not rule:
        gaps.append("không tìm thấy CRUSH rule của pool")
    if not domain_type:
        gaps.append("CRUSH rule không công bố failure domain")
    if domain_type and domains == 0:
        gaps.append(f"không tìm thấy domain '{domain_type}' trong CRUSH topology")
    if requirements and domains and domains < requirements:
        notes.append(f"Chỉ có {domains} failure domain '{domain_type}', cần tối thiểu {requirements}.")

    invalid = any("không hợp lệ" in note for note in notes)
    insufficient_domains = bool(requirements and domains and domains < requirements)
    if invalid or insufficient_domains:
        status = "CRITICAL"
    elif gaps:
        status = "INSUFFICIENT_EVIDENCE"
    elif notes:
        status = "WARNING"
    else:
        status = "HEALTHY"
    return {
        "status": status,
        "policy": policy,
        "crush": {
            "rule_id": rule_id,
            "rule_name": rule.get("rule_name") or rule.get("name"),
            "failure_domain": domain_type,
            "available_domains": domains,
            "required_domains": requirements,
        },
        "migration_required": False,
        "policy_change_supported": False,
        "notes": notes,
        "evidence": {"gaps": gaps},
        "read_only": True,
    }
