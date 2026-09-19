"""Deterministic, read-only performance recommendation simulations.

The simulator estimates impact from operator-supplied evidence.  It never
builds a Ceph command and never creates an Action.  Missing or stale evidence
returns ``INSUFFICIENT_EVIDENCE`` instead of inventing movement, benefit, or
failure-domain safety.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping


SIMULATION_TTL_SECONDS = 15 * 60
SCENARIOS = {
    "resize", "qos", "flatten", "retention", "placement", "replica_ec", "pg_change",
}
_SCENARIO_FIELDS = {
    "resize": {"current_size_gib", "proposed_size_gib", "used_gib"},
    "qos": {"current_iops", "proposed_iops", "observed_iops"},
    "flatten": {"snapshot_count", "child_count", "clone_depth"},
    "retention": {"current_retention", "proposed_retention", "snapshot_count"},
    "placement": {
        "current_replica", "proposed_replica", "used_bytes",
        "failure_domain_count", "proposed_failure_domain_count",
    },
    "replica_ec": {
        "current_replica", "proposed_replica", "current_ec_k", "current_ec_m",
        "proposed_ec_k", "proposed_ec_m", "used_bytes",
        "failure_domain_count", "proposed_failure_domain_count",
    },
    "pg_change": {"current_pg", "proposed_pg", "pool_bytes", "osd_count"},
}


def _number(values: Mapping[str, object], key: str, *, integer: bool = False) -> float | int | None:
    value = values.get(key)
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _base_result(payload: Mapping[str, object], now: datetime) -> dict:
    scenario = str(payload.get("scenario") or "")
    target = payload.get("target") if isinstance(payload.get("target"), Mapping) else {}
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), Mapping) else {}
    captured_at = _timestamp(evidence.get("captured_at"))
    source_ids = evidence.get("source_ids")
    gaps = []
    if not target.get("pool") or not target.get("image"):
        gaps.append("target pool/image is required")
    if not isinstance(source_ids, list) or not source_ids:
        gaps.append("evidence.source_ids is required")
    if captured_at is None:
        gaps.append("evidence.captured_at is required")
    elif (now - captured_at).total_seconds() > SIMULATION_TTL_SECONDS:
        gaps.append("evidence is older than the 15-minute simulation TTL")
    if scenario not in SCENARIOS:
        gaps.append(f"unsupported scenario: {scenario or 'empty'}")
    return {
        "status": "insufficient_evidence" if gaps else "ready",
        "scenario": scenario,
        "target": {"pool": target.get("pool"), "image": target.get("image")},
        "captured_at": captured_at.isoformat() + "Z" if captured_at else None,
        "evidence_source_ids": [str(item) for item in source_ids] if isinstance(source_ids, list) else [],
        "read_only": True,
        "action_id": None,
        "recommendation_mode": "SIMULATION_ONLY",
        "ttl_seconds": SIMULATION_TTL_SECONDS,
        "evidence_expires_at": (captured_at + timedelta(seconds=SIMULATION_TTL_SECONDS)).isoformat() + "Z"
        if captured_at else None,
        "expected_benefit": None,
        "rebalance": None,
        "duration": None,
        "failure_domain_risk": None,
        "evidence_gaps": gaps,
    }


def _require_fields(result: dict, values: Mapping[str, object]) -> list[str]:
    missing = [key for key in _SCENARIO_FIELDS[result["scenario"]] if _number(values, key) is None]
    if missing:
        result["status"] = "insufficient_evidence"
        result["evidence_gaps"].append("missing numeric scenario fields: " + ", ".join(sorted(missing)))
    return missing


def _domain_risk(result: dict, values: Mapping[str, object]) -> None:
    current = _number(values, "failure_domain_count", integer=True)
    proposed = _number(values, "proposed_failure_domain_count", integer=True)
    replica = _number(values, "proposed_replica", integer=True)
    if current is None or proposed is None or replica is None:
        result["failure_domain_risk"] = {"status": "unknown", "reason": "failure-domain evidence missing"}
    elif proposed < replica:
        result["failure_domain_risk"] = {
            "status": "high", "reason": "proposed replica count exceeds available failure domains",
        }
    elif proposed < current:
        result["failure_domain_risk"] = {
            "status": "elevated", "reason": "proposed placement has fewer failure domains",
        }
    else:
        result["failure_domain_risk"] = {"status": "observed_low", "reason": "no reduction in supplied failure-domain count"}


def simulate_scenario(payload: Mapping[str, object], *, now: datetime | None = None) -> dict:
    """Return a typed simulation preview; never return an executable action."""
    now = (now or datetime.utcnow()).replace(tzinfo=None)
    result = _base_result(payload, now)
    values = payload.get("values") if isinstance(payload.get("values"), Mapping) else {}
    if result["status"] != "insufficient_evidence":
        _require_fields(result, values)
    if result["status"] == "insufficient_evidence":
        result["recommendation"] = "Collect fresh, source-labelled evidence before simulation."
        return result

    scenario = result["scenario"]
    if scenario == "resize":
        current, proposed, used = (_number(values, key) for key in ("current_size_gib", "proposed_size_gib", "used_gib"))
        if proposed < used or proposed < current:
            result["status"] = "unsupported"
            result["evidence_gaps"].append("simulation only supports non-destructive expansion")
        else:
            result["expected_benefit"] = {"capacity_headroom_gib": round(proposed - used, 3), "performance_guarantee": False}
            result["rebalance"] = {"status": "not_expected_immediately", "estimated_gib": 0, "reason": "RBD expansion allocates future space; it does not relocate existing data by itself."}
            result["recommendation"] = "Review capacity headroom; resize remains an operator-approved action."
    elif scenario == "qos":
        current, proposed, observed = (_number(values, key) for key in ("current_iops", "proposed_iops", "observed_iops"))
        result["expected_benefit"] = {"iops_cap_change": round(proposed - current, 3), "contention_reduction": proposed < observed}
        result["rebalance"] = {"status": "none", "estimated_gib": 0}
        result["recommendation"] = "Compare QoS cap with measured workload and peer contention before proposing a limit."
    elif scenario == "flatten":
        children, depth, snapshots = (_number(values, key, integer=True) for key in ("child_count", "clone_depth", "snapshot_count"))
        result["expected_benefit"] = {"dependency_reduction": children == 0 and depth <= 1, "remaining_snapshots": snapshots}
        result["rebalance"] = {"status": "possible_data_movement", "estimated_gib": None}
        result["failure_domain_risk"] = {"status": "unknown", "reason": "flatten impact depends on clone chain and snapshot topology"}
        result["recommendation"] = "Review clone children and snapshots; flatten is not safe to propose without complete dependency evidence."
    elif scenario == "retention":
        current, proposed, snapshots = (_number(values, key, integer=True) for key in ("current_retention", "proposed_retention", "snapshot_count"))
        result["expected_benefit"] = {"retained_snapshot_delta": proposed - current, "possible_snapshot_reclaim": max(0, snapshots - proposed)}
        result["rebalance"] = {"status": "none", "estimated_gib": None}
        result["failure_domain_risk"] = {"status": "unknown", "reason": "snapshot size/clone dependency evidence missing"}
        result["recommendation"] = "Review backup, clone and recovery requirements before changing retention."
    elif scenario in {"placement", "replica_ec"}:
        used = _number(values, "used_bytes")
        current_replica = _number(values, "current_replica", integer=True)
        proposed_replica = _number(values, "proposed_replica", integer=True)
        factor = proposed_replica / max(current_replica, 1)
        result["expected_benefit"] = {"replica_delta": proposed_replica - current_replica, "durability_change": proposed_replica - current_replica}
        result["rebalance"] = {"status": "estimated", "estimated_bytes_moved": round(used * abs(factor - 1), 0)}
        _domain_risk(result, values)
        result["recommendation"] = "Review failure domain, capacity reserve and recovery budget before placement/replica changes."
    elif scenario == "pg_change":
        current, proposed, pool_bytes, osds = (_number(values, key, integer=True) if key in {"current_pg", "proposed_pg", "osd_count"} else _number(values, key) for key in ("current_pg", "proposed_pg", "pool_bytes", "osd_count"))
        ratio = proposed / max(current, 1)
        result["expected_benefit"] = {"pg_count_delta": proposed - current, "distribution_change": ratio}
        result["rebalance"] = {"status": "estimated", "estimated_bytes_moved": round(pool_bytes * min(1.0, abs(ratio - 1)), 0), "osd_count": osds}
        result["failure_domain_risk"] = {"status": "unknown", "reason": "CRUSH rule, autoscaler state and recovery budget not supplied"}
        result["recommendation"] = "Compare autoscaler/CRUSH state and recovery budget; PG changes can trigger rebalance."

    result["simulation_limits"] = [
        "Duration is not estimated without an observed recovery throughput sample.",
        "No Ceph command or Action is generated by this simulation.",
        "Expected benefit is directional, not a production guarantee.",
    ]
    return result
