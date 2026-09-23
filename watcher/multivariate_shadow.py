"""Bounded, read-only feature alignment for multivariate anomaly research.

CPU/RAM come from the node forecast's Loki stream. Disk observations come
from HostMetricSample. Missing or stale components are never zero-filled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


MAX_ROWS = 128
FEATURES = ("cpu_percent", "mem_percent", "disk_read_iops", "disk_write_iops", "disk_latency_ms")


@dataclass(frozen=True)
class VectorEvidence:
    rows: tuple[dict[str, float], ...]
    aligned_samples: int
    rejected_samples: int
    max_skew_seconds: float | None
    missing_features: tuple[str, ...]
    quality_status: str


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def align_host_vectors(
    loki_samples: Iterable[tuple[datetime, float, float]],
    host_samples: Iterable[object],
    *,
    max_skew_seconds: float = 300.0,
    minimum_samples: int = 12,
) -> VectorEvidence:
    """Match nearest host sample at or before each Loki timestamp.

    No future host sample is used. The result is candidate evidence only and
    cannot send alerts, select a model, or mutate learner state.
    """
    if not 0 < max_skew_seconds <= 900 or not 3 <= minimum_samples <= MAX_ROWS:
        raise ValueError("multivariate alignment bounds are invalid")
    loki = sorted(loki_samples, key=lambda item: _utc(item[0]))[-MAX_ROWS:]
    hosts = sorted(host_samples, key=lambda item: _utc(item.collected_at))[-MAX_ROWS:]
    rows: list[dict[str, float]] = []
    rejected = 0
    observed_skew: list[float] = []
    host_index = -1
    for timestamp, cpu, mem in loki:
        when = _utc(timestamp)
        while host_index + 1 < len(hosts) and _utc(hosts[host_index + 1].collected_at) <= when:
            host_index += 1
        if host_index < 0:
            rejected += 1
            continue
        host = hosts[host_index]
        skew = (when - _utc(host.collected_at)).total_seconds()
        if skew > max_skew_seconds:
            rejected += 1
            continue
        try:
            values = {
                "cpu_percent": float(cpu), "mem_percent": float(mem),
                "disk_read_iops": float(host.disk_read_iops),
                "disk_write_iops": float(host.disk_write_iops),
                "disk_latency_ms": float(host.disk_latency_ms),
            }
        except (TypeError, ValueError, AttributeError):
            rejected += 1
            continue
        if (not all(math.isfinite(value) and value >= 0 for value in values.values())
                or values["cpu_percent"] > 100 or values["mem_percent"] > 100):
            rejected += 1
            continue
        rows.append(values)
        observed_skew.append(skew)
    missing = () if hosts else ("disk_read_iops", "disk_write_iops", "disk_latency_ms")
    quality = "OK" if len(rows) >= minimum_samples and rejected == 0 else (
        "PARTIAL" if len(rows) >= minimum_samples else "INSUFFICIENT_EVIDENCE"
    )
    return VectorEvidence(tuple(rows), len(rows), rejected,
                          max(observed_skew) if observed_skew else None, missing, quality)
