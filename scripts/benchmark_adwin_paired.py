#!/usr/bin/env python3
"""Paired, read-only benchmark for River ADWIN and the current drift detector."""

from __future__ import annotations

import argparse
import json
import math
import resource
import time
from pathlib import Path

from shared.forecast_drift import DRIFT, RiverAdwinDetector, evaluate_drift


def _series(*, stable: int = 240, shifted: int = 240) -> list[float]:
    return [50.0 + 0.25 * math.sin(index / 9.0) for index in range(stable)] + [
        80.0 + 0.25 * math.sin(index / 9.0) for index in range(shifted)
    ]


def _first(values: list[bool]) -> int | None:
    for index, value in enumerate(values):
        if value:
            return index
    return None


def run(*, stable_samples: int, shifted_samples: int, window: int) -> dict[str, object]:
    values = _series(stable=stable_samples, shifted=shifted_samples)
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    adwin = RiverAdwinDetector(delta=0.002, scope_key="benchmark|node-a|cpu|h1")
    adwin_flags: list[bool] = []
    current_flags: list[bool] = []
    for index, value in enumerate(values):
        adwin_flags.append(adwin.update(value).status == DRIFT)
        if index < window * 2:
            current_flags.append(False)
            continue
        baseline = values[index - window * 2:index - window]
        recent = values[index - window:index]
        current_flags.append(evaluate_drift(
            baseline, recent,
            baseline_residuals=[0.0] * window,
            recent_residuals=[0.0] * window,
            minimum_samples=window,
            baseline_shift_threshold=5.0,
            residual_shift_threshold=5.0,
        ).status == DRIFT)
    elapsed_wall = (time.perf_counter() - started_wall) * 1000
    elapsed_cpu = (time.process_time() - started_cpu) * 1000
    rss_delta = max(0, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before_rss))
    shift_start = stable_samples
    return {
        "schema": "ceph-ai.adwin-paired-benchmark.v1",
        "detectors": {
            "river_adwin": {
                "false_drift_rate": sum(adwin_flags[:shift_start]) / shift_start,
                "detection_delay_samples": (
                    (_first(adwin_flags[shift_start:]) or 0)
                    if any(adwin_flags[shift_start:]) else None
                ),
            },
            "current_window_drift": {
                "false_drift_rate": sum(current_flags[:shift_start]) / shift_start,
                "detection_delay_samples": (
                    (_first(current_flags[shift_start:]) or 0)
                    if any(current_flags[shift_start:]) else None
                ),
            },
        },
        "config": {
            "stable_samples": stable_samples,
            "shifted_samples": shifted_samples,
            "window_samples": window,
            "same_sample_unit": True,
        },
        "resource": {
            "wall_ms": round(elapsed_wall, 3),
            "cpu_ms": round(elapsed_cpu, 3),
            "peak_rss_delta_kib": rss_delta,
        },
        "side_effects": "read-only; no alert, notification, remediation, promotion or database write",
        "selection": "evidence_only; operator approval required for detector changes",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stable-samples", type=int, default=240)
    parser.add_argument("--shifted-samples", type=int, default=240)
    parser.add_argument("--window", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.stable_samples, args.shifted_samples, args.window) < 10:
        parser.error("all sample counts must be >= 10")
    report = run(
        stable_samples=args.stable_samples,
        shifted_samples=args.shifted_samples,
        window=args.window,
    )
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
