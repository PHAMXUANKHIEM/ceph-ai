#!/usr/bin/env python3
"""NAB-compatible event scoring adapter without importing NAB runtime code."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from scripts.forecast_benchmark import _events, load_series, run_benchmark


def score(path: Path, *, labels: Path | None, history_size: int, threshold: float) -> dict[str, object]:
    points = load_series(path, labels)
    report = run_benchmark(points, history_size=history_size, threshold=threshold)
    duration_seconds = max(
        1.0,
        (points[-1].timestamp - points[0].timestamp).total_seconds() if len(points) > 1 else 1.0,
    )
    duration_days = duration_seconds / 86400.0
    events = len(_events(points))
    scored = []
    for result in report["results"]:
        item = dict(result)
        item["false_alerts_per_day"] = round(float(result["false_positives"]) / duration_days, 6)
        item["time_to_detect_seconds"] = result["mean_detection_delay_seconds"]
        item["incident_count"] = events
        item["incident_event_recall"] = result["event_recall"]
        scored.append(item)
    return {
        "schema": "ceph-ai.nab-scoring.v1",
        "scorer_version": "ceph-ai-nab-adapted-v1",
        "license_review": "No third-party NAB code imported; event-window method is an internal adapter.",
        "dataset_points": len(points),
        "incident_count": events,
        "incident_window_seconds": 3600,
        "event_group_gap_seconds": 300,
        "duration_days": round(duration_days, 6),
        "results": scored,
        "forecast_results": report["forecast_results"],
        "unavailable": report["unavailable"],
        "side_effects": "read-only; no database, alert, notification, remediation or model-state writes",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--history-size", type=int, default=24)
    parser.add_argument("--threshold", type=float, default=3.5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = score(args.input, labels=args.labels, history_size=max(8, args.history_size), threshold=args.threshold)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({
        "schema": report["schema"],
        "incident_count": report["incident_count"],
        "detectors": [item["detector"] for item in report["results"]],
        "side_effects": report["side_effects"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
