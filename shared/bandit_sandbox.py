"""Offline contextual-bandit research sandbox.

The sandbox has no Ceph/SSH/remediation capability. It can recommend a safe
action for an operator to review, but it cannot execute or promote a policy.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Iterable, Mapping


SAFE_ACTIONS = frozenset({"OBSERVE", "COLLECT_DIAGNOSTICS", "OPEN_TICKET"})


@dataclass(frozen=True)
class VerifiedActionOutcome:
    cluster_id: str
    host: str
    metric: str
    action: str
    reward: float
    verified: bool


@dataclass(frozen=True)
class BanditRecommendation:
    scope_key: str
    action: str
    confidence: float
    sample_count: int
    reason: str
    executable: bool = False


@dataclass(frozen=True)
class SandboxReport:
    dataset_checksum: str
    processed: int
    skipped_unverified: int
    recommendations: tuple[BanditRecommendation, ...]
    elapsed_ms: float
    stopped_reason: str


def _scope(item: VerifiedActionOutcome) -> str:
    return f"{item.cluster_id}|{item.host}|{item.metric}"


def run_offline_sandbox(
    outcomes: Iterable[VerifiedActionOutcome], *,
    max_samples: int = 1000, timeout_seconds: float = 1.0,
    kill_switch: bool = False,
) -> SandboxReport:
    if max_samples < 1 or timeout_seconds <= 0:
        raise ValueError("sandbox budget must be positive")
    material = [item.__dict__ for item in outcomes]
    checksum = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    started = time.perf_counter()
    if kill_switch:
        return SandboxReport(checksum, 0, 0, (), 0.0, "kill_switch")
    scores: dict[tuple[str, str], list[float]] = {}
    processed = skipped = 0
    reason = "completed"
    for item in material:
        if processed >= max_samples:
            reason = "sample_budget_exhausted"
            break
        if time.perf_counter() - started >= timeout_seconds:
            reason = "timeout"
            break
        if item["action"] not in SAFE_ACTIONS or not item["verified"]:
            skipped += 1
            continue
        key = (f'{item["cluster_id"]}|{item["host"]}|{item["metric"]}', item["action"])
        scores.setdefault(key, []).append(float(item["reward"]))
        processed += 1
    recommendations = []
    for scope in sorted({scope for scope, _action in scores}):
        candidates = [
            (action, values) for (candidate_scope, action), values in scores.items()
            if candidate_scope == scope
        ]
        if not candidates:
            continue
        action, values = max(candidates, key=lambda item: (sum(item[1]) / len(item[1]), item[0]))
        confidence = min(1.0, len(values) / 20.0)
        recommendations.append(BanditRecommendation(
            scope, action, confidence, len(values),
            "verified offline reward; operator review required",
        ))
    return SandboxReport(
        checksum, processed, skipped, tuple(recommendations),
        (time.perf_counter() - started) * 1000, reason,
    )
