"""Deterministic, multi-signal concept-drift detection for forecasts.

The detector is deliberately small and bounded. It compares two adjacent
windows from the same stream and distinguishes insufficient evidence from an
actual drift signal. It never promotes a model or opens an alert by itself.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable


INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STABLE = "STABLE"
DRIFT = "DRIFT"


@dataclass(frozen=True)
class DriftReport:
    status: str
    score: float
    sample_count: int
    baseline_shift: float | None
    residual_shift: float | None
    coverage_drop: float | None
    alert_rate_increase: float | None
    reason: str
    confidence_multiplier: float


def _finite(values: Iterable[float | None]) -> list[float]:
    return [float(value) for value in values if value is not None and math.isfinite(float(value))]


def _mean(values: Iterable[float | None]) -> float | None:
    finite = _finite(values)
    return statistics.fmean(finite) if finite else None


def _median(values: Iterable[float | None]) -> float | None:
    finite = _finite(values)
    return statistics.median(finite) if finite else None


def _rate(values: Iterable[bool | int | float]) -> float | None:
    items = list(values)
    if not items:
        return None
    return sum(bool(item) for item in items) / len(items)


def evaluate_drift(
    baseline_values: Iterable[float | None],
    recent_values: Iterable[float | None],
    *,
    baseline_residuals: Iterable[float | None] = (),
    recent_residuals: Iterable[float | None] = (),
    baseline_coverages: Iterable[float | None] = (),
    recent_coverages: Iterable[float | None] = (),
    baseline_alerts: Iterable[bool | int | float] = (),
    recent_alerts: Iterable[bool | int | float] = (),
    minimum_samples: int = 10,
    baseline_shift_threshold: float = 15.0,
    residual_shift_threshold: float = 15.0,
    coverage_drop_threshold: float = 0.20,
    alert_rate_increase_threshold: float = 0.25,
    drift_confidence_multiplier: float = 0.5,
) -> DriftReport:
    """Compare adjacent windows and return a fail-closed drift decision."""

    baseline = _finite(baseline_values)
    recent = _finite(recent_values)
    minimum = max(1, int(minimum_samples))
    if len(baseline) < minimum or len(recent) < minimum:
        return DriftReport(
            INSUFFICIENT_DATA, 0.0, len(baseline) + len(recent),
            None, None, None, None,
            f"cần tối thiểu {minimum} sample cho mỗi cửa sổ drift",
            1.0,
        )

    baseline_median = statistics.median(baseline)
    recent_median = statistics.median(recent)
    baseline_shift = abs(recent_median - baseline_median)

    baseline_residual = _mean(baseline_residuals)
    recent_residual = _mean(recent_residuals)
    residual_shift = (
        abs(recent_residual - baseline_residual)
        if baseline_residual is not None and recent_residual is not None else None
    )

    baseline_coverage = _mean(baseline_coverages)
    recent_coverage = _mean(recent_coverages)
    coverage_drop = (
        max(0.0, baseline_coverage - recent_coverage)
        if baseline_coverage is not None and recent_coverage is not None else None
    )

    baseline_alert_rate = _rate(baseline_alerts)
    recent_alert_rate = _rate(recent_alerts)
    alert_rate_increase = (
        max(0.0, recent_alert_rate - baseline_alert_rate)
        if baseline_alert_rate is not None and recent_alert_rate is not None else None
    )

    signals: list[str] = []
    score = 0.0
    if baseline_shift > max(0.0, float(baseline_shift_threshold)):
        signals.append(f"baseline shift {baseline_shift:.2f}")
        score += 1.0
    if residual_shift is not None and residual_shift > max(0.0, float(residual_shift_threshold)):
        signals.append(f"residual shift {residual_shift:.2f}")
        score += 1.0
    if coverage_drop is not None and coverage_drop > max(0.0, float(coverage_drop_threshold)):
        signals.append(f"coverage drop {coverage_drop:.3f}")
        score += 1.0
    if alert_rate_increase is not None and alert_rate_increase > max(0.0, float(alert_rate_increase_threshold)):
        signals.append(f"alert-rate increase {alert_rate_increase:.3f}")
        score += 1.0

    status = DRIFT if signals else STABLE
    return DriftReport(
        status=status,
        score=score,
        sample_count=len(baseline) + len(recent),
        baseline_shift=baseline_shift,
        residual_shift=residual_shift,
        coverage_drop=coverage_drop,
        alert_rate_increase=alert_rate_increase,
        reason="; ".join(signals) if signals else "không phát hiện drift vượt ngưỡng",
        confidence_multiplier=(
            max(0.0, min(1.0, float(drift_confidence_multiplier))) if status == DRIFT else 1.0
        ),
    )
