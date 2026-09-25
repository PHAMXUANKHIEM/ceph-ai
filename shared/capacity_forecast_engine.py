"""Capacity observation and forecast contracts for Ceph block storage.

This module is pure and bounded.  It separates physical usage from logical
provisioning and redundancy so the forecast can explain *why* capacity is
consumed.  It never sends notifications or performs remediation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping


@dataclass(frozen=True)
class CapacityObservation:
    timestamp: datetime
    entity_type: str
    entity_name: str
    physical_used_bytes: int
    physical_total_bytes: int
    provisioned_bytes: int | None
    logical_used_bytes: int | None
    snapshot_bytes: int | None
    snapshot_count: int
    replica_factor: int | None
    ec_k: int | None
    ec_m: int | None
    failure_domain_reserve_percent: float
    quality_status: str = "OK"
    snapshot_provisioned_bytes: int | None = None


@dataclass(frozen=True)
class CapacityBreakdown:
    physical_used_bytes: int
    physical_total_bytes: int
    provisioned_bytes: int | None
    logical_used_bytes: int | None
    snapshot_provisioned_bytes: int | None
    snapshot_bytes: int | None
    thin_unallocated_bytes: int | None
    redundancy_overhead_bytes: int | None
    failure_domain_reserve_bytes: int
    usable_physical_bytes: int
    physical_used_percent: float
    provisioned_percent_of_usable: float | None
    thin_provisioning_ratio: float | None
    quality_status: str


@dataclass(frozen=True)
class CapacityForecastPoint:
    timestamp: datetime
    actual_percent: float
    forecast_percent: float | None
    confidence_low: float | None
    confidence_high: float | None
    quality_status: str


def _nonnegative(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _bounded_percent(value: object) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, parsed)) if math.isfinite(parsed) else 0.0


def build_breakdown(observation: CapacityObservation) -> CapacityBreakdown:
    """Derive capacity accounting without treating logical bytes as physical."""
    physical_used = _nonnegative(observation.physical_used_bytes)
    physical_total = _nonnegative(observation.physical_total_bytes)
    provisioned = None if observation.provisioned_bytes is None else _nonnegative(observation.provisioned_bytes)
    logical_used = None if observation.logical_used_bytes is None else _nonnegative(observation.logical_used_bytes)
    snapshot_provisioned = (
        None if observation.snapshot_provisioned_bytes is None
        else _nonnegative(observation.snapshot_provisioned_bytes)
    )
    snapshots = None if observation.snapshot_bytes is None else _nonnegative(observation.snapshot_bytes)
    logical_consumption = None if logical_used is None else logical_used + (snapshots or 0)
    total_provisioned = None if provisioned is None else provisioned + (snapshot_provisioned or 0)
    thin_unallocated = (
        None if total_provisioned is None or logical_consumption is None
        else max(0, total_provisioned - logical_consumption)
    )
    reserve = int(round(physical_total * _bounded_percent(observation.failure_domain_reserve_percent) / 100.0))
    usable = max(0, physical_total - reserve)
    replica = max(1, int(observation.replica_factor or 1))
    ec_k = max(0, int(observation.ec_k or 0))
    ec_m = max(0, int(observation.ec_m or 0))
    if ec_k and ec_m:
        raw_factor = (ec_k + ec_m) / ec_k
    else:
        raw_factor = float(replica)
    redundancy_overhead = None
    if logical_consumption is not None:
        expected_raw = int(round(logical_consumption * raw_factor))
        redundancy_overhead = max(0, physical_used - expected_raw)
    physical_percent = physical_used * 100.0 / physical_total if physical_total else 0.0
    provisioned_percent = provisioned * 100.0 / usable if provisioned is not None and usable else None
    thin_ratio = logical_consumption / provisioned if provisioned and logical_consumption is not None else None
    return CapacityBreakdown(
        physical_used_bytes=physical_used,
        physical_total_bytes=physical_total,
        provisioned_bytes=provisioned,
        logical_used_bytes=logical_used,
        snapshot_provisioned_bytes=snapshot_provisioned,
        snapshot_bytes=snapshots,
        thin_unallocated_bytes=thin_unallocated,
        redundancy_overhead_bytes=redundancy_overhead,
        failure_domain_reserve_bytes=reserve,
        usable_physical_bytes=usable,
        physical_used_percent=round(physical_percent, 4),
        provisioned_percent_of_usable=round(provisioned_percent, 4) if provisioned_percent is not None else None,
        thin_provisioning_ratio=round(thin_ratio, 6) if thin_ratio is not None else None,
        quality_status=observation.quality_status,
    )


def aggregate_volume_observations(
    timestamp: datetime,
    entity_type: str,
    entity_name: str,
    rows: Iterable[Mapping[str, object]],
    *,
    physical_used_bytes: int,
    physical_total_bytes: int,
    replica_factor: int | None = None,
    ec_k: int | None = None,
    ec_m: int | None = None,
    failure_domain_reserve_percent: float = 0.0,
) -> CapacityObservation:
    """Aggregate per-volume RBD rows while preserving snapshot attribution."""
    values = list(rows)
    provisioned = sum(_nonnegative(row.get("provisioned_size")) for row in values)
    logical_used = sum(_nonnegative(row.get("used_size")) for row in values)
    snapshot_provisioned = sum(_nonnegative(row.get("snapshot_provisioned_size")) for row in values)
    snapshot = sum(_nonnegative(row.get("snapshot_used_size")) for row in values)
    snapshot_count = sum(_nonnegative(row.get("snapshot_count")) for row in values)
    quality = "OK"
    if not values:
        quality = "MISSING_VOLUME_OBSERVATION"
    elif any(
        row.get("snapshot_used_size") is None or row.get("snapshot_provisioned_size") is None
        for row in values if _nonnegative(row.get("snapshot_count"))
    ):
        quality = "PARTIAL_SNAPSHOT_USAGE"
    return CapacityObservation(
        timestamp=timestamp,
        entity_type=entity_type,
        entity_name=entity_name,
        physical_used_bytes=_nonnegative(physical_used_bytes),
        physical_total_bytes=_nonnegative(physical_total_bytes),
        provisioned_bytes=provisioned,
        logical_used_bytes=logical_used,
        snapshot_bytes=snapshot,
        snapshot_count=snapshot_count,
        replica_factor=replica_factor,
        ec_k=ec_k,
        ec_m=ec_m,
        failure_domain_reserve_percent=failure_domain_reserve_percent,
        quality_status=quality,
        snapshot_provisioned_bytes=snapshot_provisioned,
    )


def detect_counter_reset(previous: float | None, current: float | None) -> bool:
    """Detect a reset for monotonic counters without treating gauge drops as resets."""
    return previous is not None and current is not None and math.isfinite(previous) and math.isfinite(current) and current < previous


def _linear(points: list[tuple[datetime, float]], target: datetime) -> tuple[float, float] | None:
    if len(points) < 2:
        return None
    origin = points[0][0]
    xs = [(at - origin).total_seconds() / 86400 for at, _ in points]
    ys = [value for _, value in points]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator if denominator else 0.0
    intercept = mean_y - slope * mean_x
    predicted = intercept + slope * ((target - origin).total_seconds() / 86400)
    residual = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    confidence = max(0.0, min(1.0, 1.0 - math.sqrt(residual / len(ys)) / max(1.0, abs(mean_y))))
    return max(0.0, min(100.0, predicted)), confidence


def build_chart(
    observations: Iterable[CapacityObservation], *, horizon_days: int = 30,
) -> list[CapacityForecastPoint]:
    """Return bounded actual/forecast/confidence points for chart consumers."""
    rows = sorted(observations, key=lambda item: item.timestamp)
    actual = []
    for row in rows:
        breakdown = build_breakdown(row)
        actual.append((row.timestamp, breakdown.physical_used_percent, row.quality_status))
    if not actual:
        return []
    model = _linear([(at, value) for at, value, quality in actual if quality == "OK"], actual[-1][0] + timedelta(days=max(1, horizon_days)))
    chart = [CapacityForecastPoint(at, round(value, 4), None, None, None, quality) for at, value, quality in actual[-240:]]
    if model is None:
        return chart
    predicted, confidence = model
    end = actual[-1][0] + timedelta(days=max(1, horizon_days))
    chart.append(CapacityForecastPoint(end, round(actual[-1][1], 4), round(predicted, 4), round(max(0.0, predicted - (1 - confidence) * 10), 4), round(min(100.0, predicted + (1 - confidence) * 10), 4), "FORECAST"))
    return chart
