#!/usr/bin/env python3
"""Persisted paired walk-forward evidence, read-only and scope-preserving."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from shared.db import SessionLocal
from watcher.forecast_replay import compare_persisted_forecast_runs


def build_report() -> dict[str, object]:
    with SessionLocal() as session:
        comparisons = compare_persisted_forecast_runs(session)
    rows = []
    for comparison in comparisons:
        rows.append({
            "scope_type": comparison.scope_type,
            "scope_key": comparison.scope_key,
            "horizon_hours": comparison.horizon_hours,
            "active": {
                "algorithm": comparison.active_algorithm,
                "window_hours": comparison.active_window_hours,
                "evaluated": comparison.active_evaluated,
                "mae": comparison.active_mae,
                "rmse": comparison.active_rmse,
                "smape": comparison.active_smape,
                "bias": comparison.active_bias,
                "false_positive_rate": comparison.active_false_positive_rate,
            },
            "candidate": {
                "algorithm": comparison.candidate_algorithm,
                "window_hours": comparison.candidate_window_hours,
                "evaluated": comparison.candidate_evaluated,
                "mae": comparison.candidate_mae,
                "rmse": comparison.candidate_rmse,
                "smape": comparison.candidate_smape,
                "bias": comparison.candidate_bias,
                "false_positive_rate": comparison.candidate_false_positive_rate,
                "drift_status": comparison.candidate_drift_status,
                "drift_score": comparison.candidate_drift_score,
            },
            "paired": {
                "target_timestamp": comparison.latest_target_at,
                "same_scope": bool(comparison.scope_key),
                "same_horizon": comparison.horizon_hours is not None,
            },
            "status": comparison.status,
            "reason": comparison.reason,
            "execution_mode": comparison.execution_mode,
            "resource_budget_ok": comparison.resource_budget_ok,
            "poll_latency_ms": comparison.poll_latency_ms,
        })
    return {
        "schema": "ceph-ai.forecast-walk-forward.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "paired_same_target": True,
        "comparison_count": len(rows),
        "scope_count": len({row["scope_key"] for row in rows if row["scope_key"]}),
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "comparisons": rows,
        "side_effects": "read-only; no database writes, model promotion, alert, notification or remediation",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    print(json.dumps({
        "comparison_count": report["comparison_count"],
        "scope_count": report["scope_count"],
        "status_counts": report["status_counts"],
        "read_only": report["read_only"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
