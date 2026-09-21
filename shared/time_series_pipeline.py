"""Bounded normalization for capacity forecast time-series evidence.

This module is deliberately storage-agnostic: collectors may feed Ceph or
Vitastor records, while forecast code receives the same UTC/gap/reset contract.
It never writes a metric or invents a value when an input is missing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


SERIES_NAMES = frozenset({
    "raw", "used", "available", "pool_used", "thin_provisioned",
    "volume_growth", "snapshot_growth", "replication_overhead",
    "ec_overhead", "failure_domain_reserve",
})


@dataclass(frozen=True)
class NormalizedPoint:
    observed_at: datetime
    value: float


@dataclass(frozen=True)
class NormalizedSeries:
    name: str
    points: tuple[NormalizedPoint, ...]
    gap_count: int
    reset_count: int
    dropped_count: int
    quality_status: str
    evidence_start: datetime | None
    evidence_end: datetime | None


def _timestamp(value: datetime | str) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_series(
    name: str,
    samples: Iterable[dict],
    *,
    value_key: str = "value",
    expected_interval_seconds: float = 300.0,
    retention_days: int = 90,
    counter: bool | None = None,
    now: datetime | None = None,
) -> NormalizedSeries:
    """Normalize one metric with explicit gaps, resets and dropped evidence."""
    metric = str(name or "").strip().lower()
    if metric not in SERIES_NAMES:
        raise ValueError(f"unsupported capacity series: {metric!r}")
    current = _timestamp(now or datetime.now(timezone.utc))
    cutoff = current - timedelta(days=max(1, int(retention_days)))
    by_time: dict[datetime, float] = {}
    dropped = 0
    for sample in samples:
        if not isinstance(sample, dict):
            dropped += 1
            continue
        observed_at = _timestamp(sample.get("observed_at"))
        try:
            value = float(sample.get(value_key))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if observed_at is None or observed_at < cutoff or observed_at > current + timedelta(minutes=5):
            dropped += 1
            continue
        if not math.isfinite(value) or value < 0:
            dropped += 1
            continue
        by_time[observed_at] = value
    points = tuple(NormalizedPoint(at, value) for at, value in sorted(by_time.items()))
    gaps = sum(
        (right.observed_at - left.observed_at).total_seconds()
        > max(1.0, float(expected_interval_seconds)) * 2
        for left, right in zip(points, points[1:])
    )
    is_counter = metric in {"raw", "used", "available"} if counter is None else bool(counter)
    resets = sum(
        right.value < left.value for left, right in zip(points, points[1:])
    ) if is_counter else 0
    if not points:
        quality = "EMPTY"
    elif dropped and not points:
        quality = "INSUFFICIENT_EVIDENCE"
    elif gaps:
        quality = "GAP_DETECTED"
    elif resets:
        quality = "COUNTER_RESET"
    else:
        quality = "OK"
    return NormalizedSeries(
        name=metric, points=points, gap_count=gaps, reset_count=resets,
        dropped_count=dropped, quality_status=quality,
        evidence_start=points[0].observed_at if points else None,
        evidence_end=points[-1].observed_at if points else None,
    )


def normalize_capacity_snapshot(
    samples: Iterable[dict], *, now: datetime | None = None,
    retention_days: int = 90,
) -> dict[str, NormalizedSeries]:
    """Build the common capacity metric set from records with a metrics map."""
    buckets: dict[str, list[dict]] = {name: [] for name in SERIES_NAMES}
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        observed_at = sample.get("observed_at")
        metrics = sample.get("metrics")
        if not isinstance(metrics, dict):
            continue
        for name, value in metrics.items():
            if name in buckets:
                buckets[name].append({"observed_at": observed_at, "value": value})
    return {
        name: normalize_series(
            name, values, now=now, retention_days=retention_days,
            counter=name in {"raw", "used", "available"},
        )
        for name, values in buckets.items()
        if values
    }
