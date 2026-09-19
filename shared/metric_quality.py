"""Deterministic data-quality contract shared by metric learners."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from math import floor
from typing import Iterable


class MetricQualityStatus(str, Enum):
    OK = "OK"
    STALE = "STALE"
    INSUFFICIENT_SAMPLES = "INSUFFICIENT_SAMPLES"
    LOW_COVERAGE = "LOW_COVERAGE"
    GAP_DETECTED = "GAP_DETECTED"
    SOURCE_ERROR = "SOURCE_ERROR"


@dataclass(frozen=True)
class MetricQuality:
    status: str
    latest_observed_at: datetime | None
    age_seconds: float | None
    sample_count: int
    history_seconds: float
    coverage_ratio: float
    longest_gap_seconds: float
    reason: str

    @property
    def usable(self) -> bool:
        return self.status == MetricQualityStatus.OK.value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def source_error(reason: str) -> MetricQuality:
    return MetricQuality(
        status=MetricQualityStatus.SOURCE_ERROR.value,
        latest_observed_at=None,
        age_seconds=None,
        sample_count=0,
        history_seconds=0.0,
        coverage_ratio=0.0,
        longest_gap_seconds=0.0,
        reason=reason,
    )


def assess_metric_quality(
    timestamps: Iterable[datetime],
    *,
    now: datetime,
    max_age_seconds: float,
    minimum_samples: int,
    expected_interval_seconds: float,
    minimum_coverage_ratio: float,
    maximum_gap_seconds: float,
    minimum_history_seconds: float = 0.0,
) -> MetricQuality:
    """Assess freshness, history length, coverage and the longest gap."""

    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    if minimum_samples < 1:
        raise ValueError("minimum_samples must be at least 1")
    if expected_interval_seconds <= 0:
        raise ValueError("expected_interval_seconds must be positive")
    if not 0.0 <= minimum_coverage_ratio <= 1.0:
        raise ValueError("minimum_coverage_ratio must be between 0 and 1")
    if maximum_gap_seconds < 0:
        raise ValueError("maximum_gap_seconds must be non-negative")

    points = sorted({_utc(value) for value in timestamps})
    if not points:
        return MetricQuality(
            status=MetricQualityStatus.INSUFFICIENT_SAMPLES.value,
            latest_observed_at=None,
            age_seconds=None,
            sample_count=0,
            history_seconds=0.0,
            coverage_ratio=0.0,
            longest_gap_seconds=0.0,
            reason="no metric samples",
        )

    reference = _utc(now)
    latest = points[-1]
    age = max(0.0, (reference - latest).total_seconds())
    history = max(0.0, (latest - points[0]).total_seconds())
    gaps = [
        (right - left).total_seconds()
        for left, right in zip(points, points[1:])
    ]
    longest_gap = max(gaps, default=0.0)
    expected_samples = max(1, floor(history / expected_interval_seconds) + 1)
    coverage = min(1.0, len(points) / expected_samples)
    common = dict(
        latest_observed_at=latest,
        age_seconds=age,
        sample_count=len(points),
        history_seconds=history,
        coverage_ratio=coverage,
        longest_gap_seconds=longest_gap,
    )

    if age > max_age_seconds:
        status = MetricQualityStatus.STALE
        reason = f"latest sample is {age:.1f}s old; limit is {max_age_seconds:.1f}s"
    elif len(points) < minimum_samples:
        status = MetricQualityStatus.INSUFFICIENT_SAMPLES
        reason = f"{len(points)} samples; minimum is {minimum_samples}"
    elif history < minimum_history_seconds:
        status = MetricQualityStatus.INSUFFICIENT_SAMPLES
        reason = f"history is {history:.1f}s; minimum is {minimum_history_seconds:.1f}s"
    elif longest_gap > maximum_gap_seconds:
        status = MetricQualityStatus.GAP_DETECTED
        reason = f"longest gap is {longest_gap:.1f}s; limit is {maximum_gap_seconds:.1f}s"
    elif coverage < minimum_coverage_ratio:
        status = MetricQualityStatus.LOW_COVERAGE
        reason = f"coverage is {coverage:.3f}; minimum is {minimum_coverage_ratio:.3f}"
    else:
        status = MetricQualityStatus.OK
        reason = "quality checks passed"

    return MetricQuality(status=status.value, reason=reason, **common)
