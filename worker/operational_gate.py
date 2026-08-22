"""Deterministic runtime safety checks over a fresh ``ceph status`` snapshot."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OperationalGateResult:
    allowed: bool
    reason: str | None = None


def evaluate(snapshot: dict, *, max_recovery_bytes_per_sec: float = 0,
             active_latency_incidents: int = 0) -> OperationalGateResult:
    if not isinstance(snapshot, dict) or not snapshot:
        return OperationalGateResult(False, "fresh ceph status is unavailable or malformed")
    health = snapshot.get("health") or {}
    status = health.get("status") or snapshot.get("status")
    if status not in {"HEALTH_OK", "HEALTH_WARN", "HEALTH_ERR"}:
        return OperationalGateResult(False, "fresh cluster health status is unknown")
    if status == "HEALTH_ERR":
        return OperationalGateResult(False, "cluster is HEALTH_ERR")

    monmap = snapshot.get("monmap") or {}
    quorum = snapshot.get("quorum_names")
    expected_mons = int(monmap.get("num_mons") or len(monmap.get("mons") or []))
    if expected_mons and (not isinstance(quorum, list) or len(quorum) < expected_mons // 2 + 1):
        return OperationalGateResult(False, f"MON quorum is unsafe ({len(quorum or [])}/{expected_mons})")

    pgmap = snapshot.get("pgmap") or {}
    for row in pgmap.get("pgs_by_state") or []:
        state = str(row.get("state_name") or "").lower()
        count = int(row.get("count") or 0)
        if count and any(token in state.split("+") for token in ("inactive", "incomplete", "stale")):
            return OperationalGateResult(False, f"{count} PG(s) are {state}")

    recovery_bps = float(pgmap.get("recovering_bytes_per_sec") or 0)
    if recovery_bps > max(0, max_recovery_bytes_per_sec):
        return OperationalGateResult(
            False, f"recovery rate {recovery_bps:.0f} B/s exceeds {max_recovery_bytes_per_sec:.0f} B/s"
        )
    if active_latency_incidents:
        return OperationalGateResult(False, f"{active_latency_incidents} active OSD latency incident(s)")
    return OperationalGateResult(True)
