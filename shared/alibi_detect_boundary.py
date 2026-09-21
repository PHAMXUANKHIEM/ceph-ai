"""Bounded anomaly evidence boundary for an optional Alibi Detect adapter."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable


EXPECTED_SCHEMA = "scalar-v1"


@dataclass(frozen=True)
class DetectorEvidence:
    scope_key: str | None
    detector: str
    status: str
    score: float | None
    sample_count: int
    reason: str
    false_positive: bool | None = None
    detection_delay_samples: int | None = None


def detect_outlier(
    values: Iterable[float], *, baseline: Iterable[float], scope_key: str | None,
    feature_schema: str, threshold_sigma: float = 3.0,
) -> DetectorEvidence:
    """Return evidence only; lifecycle/notification decisions stay elsewhere."""

    if not scope_key or feature_schema != EXPECTED_SCHEMA:
        return DetectorEvidence(scope_key, "alibi-detect-boundary", "DATA_QUALITY", None, 0,
                                "missing scope or feature schema mismatch")
    base = [float(item) for item in baseline if math.isfinite(float(item))]
    recent = [float(item) for item in values if math.isfinite(float(item))]
    if len(base) < 2 or not recent:
        return DetectorEvidence(scope_key, "alibi-detect-boundary", "INSUFFICIENT_DATA", None,
                                len(recent), "baseline or recent window is insufficient")
    mean = statistics.fmean(base)
    deviation = statistics.pstdev(base)
    scale = deviation if deviation > 0 else max(abs(mean) * 0.01, 1e-9)
    scores = [abs(item - mean) / scale for item in recent]
    score = max(scores)
    return DetectorEvidence(
        scope_key, "alibi-detect-boundary", "DRIFT" if score >= threshold_sigma else "STABLE",
        score, len(recent), "bounded z-score evidence", detection_delay_samples=next(
            (index + 1 for index, item in enumerate(scores) if item >= threshold_sigma), None,
        ),
    )
