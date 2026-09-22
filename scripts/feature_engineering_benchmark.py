#!/usr/bin/env python3
"""Offline resource benchmark for the bounded feature builder.

The benchmark only creates in-memory synthetic points.  It does not access
Ceph, SSH, SQLAlchemy, model registry, alerts, Telegram, or remediation.
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from datetime import datetime, timedelta, timezone

from shared.forecast_features import MetricPoint, build_features


def benchmark(sample_count: int, repeats: int = 3) -> dict[str, int | float]:
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    points = [
        MetricPoint(origin + timedelta(minutes=index * 5), float(index % 100))
        for index in range(sample_count)
    ]
    observed_at = points[-1].observed_at
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    cpu_started = time.process_time()
    result = None
    for _ in range(max(1, repeats)):
        result = build_features(
            points,
            observed_at=observed_at,
            expected_interval_seconds=300,
            max_gap_seconds=900,
            metric="cpu",
            horizon_hours=1,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    cpu_ms = (time.process_time() - cpu_started) * 1000
    after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    state = {
        "features": result.features,
        "feature_schema": result.feature_schema,
        "sample_count": result.sample_count,
        "coverage_ratio": result.coverage_ratio,
        "max_gap_seconds": result.max_gap_seconds,
        "missing_features": result.missing_features,
        "quality_status": result.quality_status,
    }
    state_bytes = len(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {
        "sample_count": sample_count,
        "repeats": max(1, repeats),
        "wall_ms": round(elapsed_ms, 3),
        "cpu_ms": round(cpu_ms, 3),
        "peak_rss_delta_kib": max(0, int(after_rss - before_rss)),
        "state_bytes": state_bytes,
        "feature_count": len(result.features),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    print(json.dumps([
        benchmark(sample_count, args.repeats)
        for sample_count in (100, 1000, 5000)
    ], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
