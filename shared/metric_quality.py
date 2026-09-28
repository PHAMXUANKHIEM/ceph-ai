"""Shared data-quality contract for metric streams.

The forecasting pipeline must distinguish a real healthy result from a result
that is unavailable because the source is stale, sparse, or interrupted.
This module is intentionally deterministic and has no database or network
dependency so every watcher can use the same rules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
    """Auditable quality result for one metric time series."""

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

    def as_dict(self) -> dict:
        return asdict(self)


def _as_utc(value: datetime) -> datetime:
    """Normalize naive application timestamps as UTC by contract."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _quality(
    status: MetricQualityStatus,
    *,
    latest_observed_at: datetime | None = None,
    age_seconds: float | None = None,
    sample_count: int = 0,
    history_seconds: float = 0.0,
    coverage_ratio: float = 0.0,
    longest_gap_seconds: float = 0.0,
    reason: str,
) -> MetricQuality:
    return MetricQuality(
        status=status.value,
        latest_observed_at=latest_observed_at,
        age_seconds=age_seconds,
        sample_count=sample_count,
        history_seconds=round(max(0.0, history_seconds), 3),
        coverage_ratio=round(max(0.0, min(1.0, coverage_ratio)), 6),
        longest_gap_seconds=round(max(0.0, longest_gap_seconds), 3),
        reason=reason,
    )


def source_error(reason: str) -> MetricQuality:
    """Return a non-usable quality result for an upstream source failure."""

    return _quality(MetricQualityStatus.SOURCE_ERROR, reason=reason)


def assess_metric_quality(
    timestamps: Iterable[datetime],
    *,
    now: datetime | None = None,
    max_age_seconds: float,
    minimum_samples: int,
    expected_interval_seconds: float | None = None,
    minimum_coverage_ratio: float = 0.0,
    maximum_gap_seconds: float | None = None,
    minimum_history_seconds: float = 0.0,
) -> MetricQuality:
    """Assess freshness, sample sufficiency, coverage, and gaps.

    timestamps may contain duplicates; duplicate timestamps count once.
    Naive timestamps are interpreted as UTC because the application's database
    timestamps are stored without timezone information. The caller should pass
    timestamps only, not metric values.

    Status priority is intentional: no samples -> insufficient, stale newest
    sample -> stale, then history/sample count, gap, and coverage. A stale
    stream must never be treated as healthy merely because it contains many
    historical points.
    """

    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    if minimum_samples < 1:
        raise ValueError("minimum_samples must be at least 1")
    if expected_interval_seconds is not None and expected_interval_seconds <= 0:
        raise ValueError("expected_interval_seconds must be positive")
    if not 0.0 <= minimum_coverage_ratio <= 1.0:
        raise ValueError("minimum_coverage_ratio must be between 0 and 1")
    if maximum_gap_seconds is not None and maximum_gap_seconds < 0:
        raise ValueError("maximum_gap_seconds must be non-negative")
    if minimum_history_seconds < 0:
        raise ValueError("minimum_history_seconds must be non-negative")

    unique = sorted({_as_utc(value) for value in timestamps})
    if not unique:
        return _quality(
            MetricQualityStatus.INSUFFICIENT_SAMPLES,
            reason="no valid metric samples",
        )

    reference = _as_utc(now or datetime.now(timezone.utc))
    latest = unique[-1]
    age_seconds = max(0.0, (reference - latest).total_seconds())
    history_seconds = max(0.0, (latest - unique[0]).total_seconds())
    gaps = [
        (right - left).total_seconds()
        for left, right in zip(unique, unique[1:])
    ]
    longest_gap_seconds = max(gaps, default=0.0)

    if expected_interval_seconds:
        expected_samples = max(
            1,
            floor(history_seconds / expected_interval_seconds) + 1,
        )
        coverage_ratio = min(1.0, len(unique) / expected_samples)
    else:
        coverage_ratio = 1.0

    common = dict(
        latest_observed_at=latest,
        age_seconds=age_seconds,
        sample_count=len(unique),
        history_seconds=history_seconds,
        coverage_ratio=coverage_ratio,
        longest_gap_seconds=longest_gap_seconds,
    )

    if age_seconds > max_age_seconds:
        return _quality(
            MetricQualityStatus.STALE,
            **common,
            reason=f"latest sample is {age_seconds:.1f}s old; limit is {max_age_seconds:.1f}s",
        )
    if len(unique) < minimum_samples:
        return _quality(
            MetricQualityStatus.INSUFFICIENT_SAMPLES,
            **common,
            reason=f"{len(unique)} samples; minimum is {minimum_samples}",
        )
    if history_seconds < minimum_history_seconds:
        return _quality(
            MetricQualityStatus.INSUFFICIENT_SAMPLES,
            **common,
            reason=f"history is {history_seconds:.1f}s; minimum is {minimum_history_seconds:.1f}s",
        )
    if maximum_gap_seconds is not None and longest_gap_seconds > maximum_gap_seconds:
        return _quality(
            MetricQualityStatus.GAP_DETECTED,
            **common,
            reason=f"longest gap is {longest_gap_seconds:.1f}s; limit is {maximum_gap_seconds:.1f}s",
        )
    if coverage_ratio < minimum_coverage_ratio:
        return _quality(
            MetricQualityStatus.LOW_COVERAGE,
            **common,
            reason=f"coverage is {coverage_ratio:.3f}; minimum is {minimum_coverage_ratio:.3f}",
        )

    return _quality(MetricQualityStatus.OK, **common, reason="quality checks passed")

