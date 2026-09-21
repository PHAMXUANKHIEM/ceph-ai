"""Bounded, leakage-safe feature engineering for resource forecasts."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence


@dataclass(frozen=True)
class MetricPoint:
    observed_at: datetime
    value: float


@dataclass(frozen=True)
class FeatureSet:
    features: dict[str, float]
    feature_schema: str
    observed_at: datetime
    sample_count: int
    coverage_ratio: float
    max_gap_seconds: float
    missing_features: tuple[str, ...]
    quality_status: str


FEATURE_SCHEMA = "resource-v2"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finite(points: Iterable[MetricPoint]) -> list[MetricPoint]:
    ordered = sorted(points, key=lambda point: _utc(point.observed_at))
    return [point for point in ordered if math.isfinite(float(point.value))]


def _slope(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    x_mean = (len(values) - 1) / 2
    y_mean = statistics.fmean(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    return sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / denominator


def build_features(
    points: Iterable[MetricPoint],
    *,
    observed_at: datetime | None = None,
    expected_interval_seconds: float = 300.0,
    max_gap_seconds: float = 900.0,
) -> FeatureSet:
    """Build deterministic features using samples strictly before ``observed_at``.

    No forward fill is performed.  A gap is reported as a quality failure and
    missing lags remain explicit in ``missing_features``.
    """

    ordered = _finite(points)
    if observed_at is not None:
        cutoff = _utc(observed_at)
        ordered = [point for point in ordered if _utc(point.observed_at) <= cutoff]
    if not ordered:
        when = _utc(observed_at or datetime.now(timezone.utc))
        return FeatureSet({}, FEATURE_SCHEMA, when, 0, 0.0, 0.0, ("current",), "INSUFFICIENT_SAMPLES")

    when = _utc(observed_at or ordered[-1].observed_at)
    values = [float(point.value) for point in ordered]
    timestamps = [_utc(point.observed_at) for point in ordered]
    gaps = [max(0.0, (right - left).total_seconds()) for left, right in zip(timestamps, timestamps[1:])]
    longest_gap = max(gaps or [0.0])
    expected = max(1.0, float(expected_interval_seconds))
    span = max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())
    expected_count = max(1.0, span / expected + 1.0)
    coverage = min(1.0, len(values) / expected_count)
    features: dict[str, float] = {"current": values[-1]}
    missing: list[str] = []
    for lag in (1, 3, 6, 12, 24):
        key = f"lag_{lag}"
        if len(values) > lag:
            features[key] = values[-1 - lag]
        else:
            missing.append(key)
    for window in (6, 24):
        key_mean = f"rolling_mean_{window}"
        key_std = f"rolling_std_{window}"
        window_values = values[-window:]
        if len(window_values) >= min(window, 3):
            features[key_mean] = statistics.fmean(window_values)
            features[key_std] = statistics.pstdev(window_values)
        else:
            missing.extend((key_mean, key_std))
    slope = _slope(values[-6:])
    if slope is None:
        missing.append("slope_6")
    else:
        features["slope_6"] = slope
    features["calendar_hour_sin"] = math.sin(2 * math.pi * (when.hour + when.minute / 60) / 24)
    features["calendar_hour_cos"] = math.cos(2 * math.pi * (when.hour + when.minute / 60) / 24)
    features["calendar_weekday_sin"] = math.sin(2 * math.pi * when.weekday() / 7)
    features["calendar_weekday_cos"] = math.cos(2 * math.pi * when.weekday() / 7)
    quality = "OK"
    if longest_gap > max(0.0, float(max_gap_seconds)):
        quality = "GAP_DETECTED"
    elif len(values) < 3 or missing:
        quality = "INSUFFICIENT_SAMPLES"
    elif coverage < 0.8:
        quality = "LOW_COVERAGE"
    return FeatureSet(
        features=features,
        feature_schema=FEATURE_SCHEMA,
        observed_at=when,
        sample_count=len(values),
        coverage_ratio=coverage,
        max_gap_seconds=longest_gap,
        missing_features=tuple(sorted(set(missing))),
        quality_status=quality,
    )
