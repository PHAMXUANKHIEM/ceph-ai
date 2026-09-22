#!/usr/bin/env python3
"""Offline Alibi Detect vs River ADWIN benchmark; evidence only."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import resource
import time
import warnings

os.environ.setdefault("OMP_NUM_THREADS", "1")


def run_benchmark(*, reference_size: int = 200, chunk_size: int = 20) -> dict[str, object]:
    try:
        import numpy as np
        from alibi_detect.cd import KSDrift
        from river.drift import ADWIN
    except ImportError as exc:  # pragma: no cover - evaluation venv only
        raise RuntimeError("Alibi benchmark requires the isolated evaluation extra") from exc

    rng = np.random.default_rng(11)
    reference = rng.normal(50, 2, reference_size)
    stable = rng.normal(50, 2, chunk_size * 5)
    drifted = rng.normal(70, 2, chunk_size * 5)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    with warnings.catch_warnings(record=True) as captured, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        alibi = KSDrift(reference.reshape(-1, 1), p_val=0.01)
        alibi_stable = [
            bool(alibi.predict(stable[index:index + chunk_size].reshape(-1, 1))["data"]["is_drift"])
            for index in range(0, len(stable), chunk_size)
        ]
        alibi_drift = [
            bool(alibi.predict(drifted[index:index + chunk_size].reshape(-1, 1))["data"]["is_drift"])
            for index in range(0, len(drifted), chunk_size)
        ]
        adwin = ADWIN(delta=0.002)
        adwin_stable = []
        for value in stable:
            adwin.update(float(value))
            adwin_stable.append(bool(adwin.drift_detected))
        adwin_drift = []
        for value in drifted:
            adwin.update(float(value))
            adwin_drift.append(bool(adwin.drift_detected))
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    def first_true(values: list[bool]) -> int | None:
        return next((index + 1 for index, value in enumerate(values) if value), None)

    return {
        "schema": "ceph-ai.alibi-detect-benchmark.v1",
        "alibi_detect_version": "0.13.0",
        "reference_size": reference_size,
        "chunk_size": chunk_size,
        "alibi": {
            "stable_false_positive_rate": sum(alibi_stable) / len(alibi_stable),
            "drift_detection_delay_chunks": first_true(alibi_drift),
        },
        "river_adwin": {
            "stable_false_positive_rate": sum(adwin_stable) / len(adwin_stable),
            "drift_detection_delay_samples": first_true(adwin_drift),
        },
        "wall_ms": round((time.perf_counter() - wall_started) * 1000, 3),
        "cpu_ms": round((time.process_time() - cpu_started) * 1000, 3),
        "rss_delta_kib": max(0, after - before),
        "warnings": len(captured),
        "production_dependency": False,
        "lifecycle_side_effects": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = run_benchmark()
    with open(args.output, "x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
