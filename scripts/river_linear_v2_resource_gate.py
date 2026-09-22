#!/usr/bin/env python3
"""Measure the bounded resource profile of the River linear v2 adapter."""

from __future__ import annotations

import argparse
import json
import resource
import time
from pathlib import Path

from shared.river_linear_v2 import RiverLinearV2


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.999999) - 1))
    return ordered[index]


def measure(
    *,
    samples: int,
    max_wall_ms: float,
    max_cpu_ms: float,
    max_rss_delta_kib: int,
) -> dict[str, object]:
    if samples < 1:
        raise ValueError("samples must be positive")
    feature_seed = {"current": 10.0, "lag_1": 9.0, "rolling_mean_6": 8.0}
    learner = RiverLinearV2()
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for index in range(samples):
        features = {key: value + index * 0.001 for key, value in feature_seed.items()}
        learner.learn_one(features, 10.0 + index * 0.01, outcome="VERIFIED_SUCCESS")
    snapshot = learner.snapshot()
    after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    wall_ms = (time.perf_counter() - started_wall) * 1000
    cpu_ms = (time.process_time() - started_cpu) * 1000
    rss_delta_kib = max(0, int(after_rss - before_rss))
    checks = {
        "state_bytes": len(encoded) <= 64 * 1024,
        "wall_ms": wall_ms <= max_wall_ms,
        "cpu_ms": cpu_ms <= max_cpu_ms,
        "peak_rss_delta_kib": rss_delta_kib <= max_rss_delta_kib,
    }
    return {
        "algorithm": "river_linear_v2",
        "samples": samples,
        "wall_ms": round(wall_ms, 3),
        "cpu_ms": round(cpu_ms, 3),
        "peak_rss_delta_kib": rss_delta_kib,
        "state_bytes": len(encoded),
        "budgets": {
            "max_wall_ms": max_wall_ms,
            "max_cpu_ms": max_cpu_ms,
            "max_peak_rss_delta_kib": max_rss_delta_kib,
            "max_state_bytes": 64 * 1024,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=5000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--max-wall-ms", type=float, default=2000.0)
    parser.add_argument("--max-cpu-ms", type=float, default=2000.0)
    parser.add_argument("--max-rss-delta-kib", type=int, default=65536)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    runs = [measure(
        samples=args.samples,
        max_wall_ms=args.max_wall_ms,
        max_cpu_ms=args.max_cpu_ms,
        max_rss_delta_kib=args.max_rss_delta_kib,
    ) for _ in range(args.repeats)]
    p95 = {
        "wall_ms": round(_p95([float(run["wall_ms"]) for run in runs]), 3),
        "cpu_ms": round(_p95([float(run["cpu_ms"]) for run in runs]), 3),
        "peak_rss_delta_kib": max(int(run["peak_rss_delta_kib"]) for run in runs),
        "state_bytes": max(int(run["state_bytes"]) for run in runs),
    }
    report = {
        "algorithm": "river_linear_v2",
        "samples": args.samples,
        "repeats": args.repeats,
        "p95": p95,
        "budgets": {
            "max_wall_ms": args.max_wall_ms,
            "max_cpu_ms": args.max_cpu_ms,
            "max_peak_rss_delta_kib": args.max_rss_delta_kib,
            "max_state_bytes": 64 * 1024,
        },
        "checks": {
            "wall_ms": p95["wall_ms"] <= args.max_wall_ms,
            "cpu_ms": p95["cpu_ms"] <= args.max_cpu_ms,
            "peak_rss_delta_kib": p95["peak_rss_delta_kib"] <= args.max_rss_delta_kib,
            "state_bytes": p95["state_bytes"] <= 64 * 1024,
        },
        "runs": runs,
    }
    report["passed"] = all(report["checks"].values()) and all(run["passed"] for run in runs)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8", errors="strict")
    print(encoded, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
