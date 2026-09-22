#!/usr/bin/env python3
"""Offline resource gate for the River SNARIMAX shadow adapter.

This benchmark never opens a Ceph connection, writes the database, emits an
alert, or promotes a model.  It measures the exact bounded adapter used by
the watcher shadow job for the three supported metric scopes.
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared.river_snarimax import SNARIMAX_PROFILES, SnarimaxShadowModel, profile_for


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1))
    return ordered[index]


def _points(metric: str, samples: int) -> list[tuple[datetime, float]]:
    values = []
    for index in range(samples):
        seasonal = math.sin(2 * math.pi * (index % 24) / 24)
        if metric == "cpu":
            value = 50.0 + 8.0 * seasonal + index * 0.02
        elif metric == "ram":
            value = 65.0 + 4.0 * seasonal + index * 0.01
        else:
            value = 100.0 + 20.0 * seasonal + index * 0.05
        values.append((ORIGIN + timedelta(hours=index), value))
    return values


def measure(metric: str, *, samples: int, timeout_seconds: float) -> dict[str, object]:
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = SnarimaxShadowModel(profile_for(metric)).fit(
        _points(metric, samples), timeout_seconds=timeout_seconds,
    )
    after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    snapshot_bytes = result.state_bytes
    return {
        "metric": metric,
        "status": result.status,
        "samples": result.sample_count,
        "training_duration_ms": result.training_duration_ms,
        "wall_ms": round((time.perf_counter() - started_wall) * 1000, 3),
        "cpu_ms": round((time.process_time() - started_cpu) * 1000, 3),
        "peak_rss_delta_kib": max(0, int(after_rss - before_rss)),
        "state_bytes": snapshot_bytes,
        "interval_samples": result.interval_sample_count,
        "profile": {
            key: getattr(profile_for(metric), key)
            for key in ("p", "d", "q", "m", "sp", "sd", "sq", "horizon_steps")
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=72)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=3.0)
    parser.add_argument("--max-wall-ms", type=float, default=3000.0)
    parser.add_argument("--max-cpu-ms", type=float, default=3000.0)
    parser.add_argument("--max-rss-delta-kib", type=int, default=65536)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.samples < 48 or args.repeats < 1:
        parser.error("--samples must be >= 48 and --repeats must be positive")

    runs = [
        measure(metric, samples=args.samples, timeout_seconds=args.timeout_seconds)
        for _ in range(args.repeats)
        for metric in SNARIMAX_PROFILES
    ]
    by_metric = {
        metric: [run for run in runs if run["metric"] == metric]
        for metric in SNARIMAX_PROFILES
    }
    summary = {}
    for metric, metric_runs in by_metric.items():
        summary[metric] = {
            "p95_wall_ms": _p95([float(run["wall_ms"]) for run in metric_runs]),
            "p95_cpu_ms": _p95([float(run["cpu_ms"]) for run in metric_runs]),
            "peak_rss_delta_kib": max(int(run["peak_rss_delta_kib"]) for run in metric_runs),
            "max_state_bytes": max(int(run["state_bytes"]) for run in metric_runs),
            "all_shadow_only": all(run["status"] == "SHADOW_ONLY" for run in metric_runs),
        }
    checks = {
        "all_shadow_only": all(item["all_shadow_only"] for item in summary.values()),
        "wall_budget": all(item["p95_wall_ms"] <= args.max_wall_ms for item in summary.values()),
        "cpu_budget": all(item["p95_cpu_ms"] <= args.max_cpu_ms for item in summary.values()),
        "rss_budget": all(item["peak_rss_delta_kib"] <= args.max_rss_delta_kib for item in summary.values()),
        "snapshot_budget": all(item["max_state_bytes"] <= 128 * 1024 for item in summary.values()),
    }
    report = {
        "algorithm": "river_snarimax",
        "backend": "river",
        "samples": args.samples,
        "repeats": args.repeats,
        "summary": summary,
        "budgets": {
            "max_wall_ms": args.max_wall_ms,
            "max_cpu_ms": args.max_cpu_ms,
            "max_peak_rss_delta_kib": args.max_rss_delta_kib,
            "max_state_bytes": 128 * 1024,
            "model_timeout_seconds": args.timeout_seconds,
        },
        "checks": checks,
        "runs": runs,
        "passed": all(checks.values()),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
