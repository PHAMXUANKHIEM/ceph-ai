#!/usr/bin/env python3
"""Offline NannyML DLE benchmark; never import this from Watcher runtime."""

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
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")


def run_benchmark(*, reference_rows: int = 200, analysis_rows: int = 100) -> dict[str, object]:
    try:
        import numpy as np
        import pandas as pd
        import nannyml as nml
    except ImportError as exc:  # pragma: no cover - exercised in evaluation venv
        raise RuntimeError("NannyML benchmark requires the isolated evaluation extra") from exc

    rng = np.random.default_rng(7)

    def frame(size: int, offset: int) -> pd.DataFrame:
        actual = np.clip(50 + rng.normal(0, 5, size), 0, 100)
        predicted = np.clip(actual + rng.normal(0, 2, size), 0, 100)
        return pd.DataFrame({
            "feature_cpu": actual / 100,
            "feature_ram": np.clip(actual / 100 + rng.normal(0, .02, size), 0, 1),
            "y_pred": predicted,
            "y_true": actual,
            "timestamp": pd.date_range("2026-01-01", periods=size, freq="h")
            + pd.Timedelta(hours=offset),
        })

    reference = frame(reference_rows, 0)
    analysis = frame(analysis_rows, reference_rows)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    with warnings.catch_warnings(record=True) as captured, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        estimator = nml.DLE(
            feature_column_names=["feature_cpu", "feature_ram"],
            y_pred="y_pred", y_true="y_true", timestamp_column_name="timestamp",
            metrics=["mae", "rmse"], chunk_size=max(50, analysis_rows // 2),
            hyperparameters={"verbosity": -1},
        )
        estimator.fit(reference)
        result = estimator.estimate(analysis)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "schema": "ceph-ai.nannyml-benchmark.v1",
        "nannyml_version": getattr(nml, "__version__", "unknown"),
        "reference_rows": reference_rows,
        "analysis_rows": analysis_rows,
        "chunks": len(result.data),
        "wall_ms": round((time.perf_counter() - wall_started) * 1000, 3),
        "cpu_ms": round((time.process_time() - cpu_started) * 1000, 3),
        "rss_delta_kib": max(0, after - before),
        "warnings": len(captured),
        "production_dependency": False,
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
