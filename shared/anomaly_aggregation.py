"""Pure aggregation contract for bounded anomaly candidate evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


@dataclass(frozen=True)
class AnomalyCandidate:
    scope_key: str
    observed_at: datetime
    raw_score: float
    features: Mapping[str, float]
    execution_mode: str = "SHADOW_ONLY"


@dataclass(frozen=True)
class AnomalyIncident:
    scope_key: str
    started_at: datetime
    ended_at: datetime
    max_score: float
    candidate_count: int
    top_features: tuple[str, ...]
    event_type: str = "ANOMALY_CANDIDATE"
    execution_mode: str = "SHADOW_ONLY"


def normalize_score(raw_score: float, baseline_scores: list[float]) -> float:
    """Return a bounded percentile score; baseline is required for calibration."""
    score = float(raw_score)
    baseline = [float(value) for value in baseline_scores if math.isfinite(float(value))]
    if not math.isfinite(score) or not baseline:
        return 0.0
    return round(sum(value <= score for value in baseline) / len(baseline), 6)


def aggregate_candidates(
    candidates: list[AnomalyCandidate], *, incident_gap_seconds: float = 300.0,
    top_feature_count: int = 3,
) -> list[AnomalyIncident]:
    """Group one scope's adjacent candidate points without creating side effects."""
    if incident_gap_seconds < 0 or top_feature_count < 1:
        raise ValueError("aggregation bounds are invalid")
    ordered = sorted(
        (candidate for candidate in candidates if candidate.execution_mode == "SHADOW_ONLY"),
        key=lambda candidate: (candidate.scope_key, candidate.observed_at),
    )
    incidents: list[AnomalyIncident] = []
    bucket: list[AnomalyCandidate] = []
    for candidate in ordered:
        if bucket and (
            candidate.scope_key != bucket[-1].scope_key
            or (candidate.observed_at - bucket[-1].observed_at).total_seconds() > incident_gap_seconds
        ):
            incidents.append(_incident(bucket, top_feature_count))
            bucket = []
        bucket.append(candidate)
    if bucket:
        incidents.append(_incident(bucket, top_feature_count))
    return incidents


def _incident(bucket: list[AnomalyCandidate], top_feature_count: int) -> AnomalyIncident:
    feature_scores: dict[str, float] = {}
    for candidate in bucket:
        for name, value in candidate.features.items():
            numeric = float(value)
            if math.isfinite(numeric):
                feature_scores[name] = max(feature_scores.get(name, 0.0), abs(numeric))
    top_features = tuple(
        name for name, _value in sorted(feature_scores.items(), key=lambda item: (-item[1], item[0]))[:top_feature_count]
    )
    return AnomalyIncident(
        scope_key=bucket[0].scope_key,
        started_at=bucket[0].observed_at,
        ended_at=bucket[-1].observed_at,
        max_score=max(float(candidate.raw_score) for candidate in bucket),
        candidate_count=len(bucket),
        top_features=top_features,
    )
