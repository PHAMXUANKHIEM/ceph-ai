#!/usr/bin/env python3
"""Generate read-only shadow-soak evidence from persisted forecast outcomes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from shared.canary_soak import evaluate_shadow_soak
from shared.db import SessionLocal
from watcher.forecast_replay import compare_persisted_forecast_runs


def build_report(*, minimum_evaluations: int = 20, minimum_duration_hours: float = 24.0) -> dict[str, object]:
    with SessionLocal() as session:
        comparisons = compare_persisted_forecast_runs(session)
    soak = evaluate_shadow_soak(
        comparisons,
        minimum_evaluations=minimum_evaluations,
        minimum_duration_hours=minimum_duration_hours,
    )
    return {
        "schema": "ceph-ai.forecast-shadow-soak.v1",
        "read_only": True,
        "comparison_count": len(comparisons),
        "soak": soak,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minimum-evaluations", type=int, default=20)
    parser.add_argument("--minimum-duration-hours", type=float, default=24.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(
        minimum_evaluations=args.minimum_evaluations,
        minimum_duration_hours=args.minimum_duration_hours,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    args.output.write_text(encoded, encoding="utf-8", errors="strict")
    print(encoded, end="")
    return 0 if report["soak"]["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
