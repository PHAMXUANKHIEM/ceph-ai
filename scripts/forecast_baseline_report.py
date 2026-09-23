#!/usr/bin/env python3
"""Create immutable, read-only forecast baseline evidence from runtime data."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from shared.db import SessionLocal


def _metrics(rows: list[dict], *, value_key: str, actual_key: str, alert_threshold: float | None) -> dict[str, object]:
    labeled = [row for row in rows if row[actual_key] is not None]
    errors = [float(row[value_key]) - float(row[actual_key]) for row in labeled]
    absolute = [abs(error) for error in errors]
    smape = [
        0.0 if abs(float(row[value_key])) + abs(float(row[actual_key])) == 0
        else 200.0 * abs(error) / (abs(float(row[value_key])) + abs(float(row[actual_key])))
        for row, error in zip(labeled, errors)
    ]
    false_positives = 0
    actual_negatives = 0
    alert_volume = 0
    if alert_threshold is not None:
        for row in labeled:
            predicted_alert = float(row[value_key]) >= alert_threshold
            actual_alert = float(row[actual_key]) >= alert_threshold
            alert_volume += int(predicted_alert)
            actual_negatives += int(not actual_alert)
            false_positives += int(predicted_alert and not actual_alert)
    quality_failures = 0
    for row in rows:
        coverage = row.get("coverage_ratio")
        max_gap = row.get("max_gap_hours")
        quality_failed = (
            coverage is not None and float(coverage) < 1.0
        ) or (
            max_gap is not None and float(max_gap) > 2.0
        )
        if quality_failed:
            quality_failures += 1
    return {
        "rows": len(rows),
        "labeled_rows": len(labeled),
        "label_rate": round(len(labeled) / len(rows), 6) if rows else 0.0,
        "mae": round(sum(absolute) / len(absolute), 6) if errors else None,
        "rmse": round(math.sqrt(sum(error * error for error in errors) / len(errors)), 6) if errors else None,
        "smape": round(sum(smape) / len(smape), 6) if smape else None,
        "bias": round(sum(errors) / len(errors), 6) if errors else None,
        "false_positive_rate": round(false_positives / actual_negatives, 6) if actual_negatives else None,
        "false_positives": false_positives,
        "alert_volume": alert_volume,
        "data_quality_failure_rate": round(quality_failures / len(rows), 6) if rows else 0.0,
    }


def _time_quality(rows: list[dict]) -> dict[str, object]:
    timestamps = sorted(row["predicted_at"] for row in rows if row["predicted_at"] is not None)
    intervals = [
        (right - left).total_seconds()
        for left, right in zip(timestamps, timestamps[1:])
        if right > left
    ]
    if not intervals:
        return {"sample_interval_seconds": None, "history_length": len(rows), "gap_count": 0, "gap_rate": 0.0}
    ordered = sorted(intervals)
    median_interval = ordered[len(ordered) // 2]
    gap_count = sum(interval > median_interval * 2 for interval in intervals)
    return {
        "sample_interval_seconds": median_interval,
        "history_length": len(rows),
        "gap_count": gap_count,
        "gap_rate": round(gap_count / len(intervals), 6),
    }


def _scope_report(rows: list[dict], *, value_key: str, actual_key: str, threshold: float | None) -> dict[str, object]:
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    # Re-run the bounded metric calculation as the active-baseline cost sample.
    metrics = _metrics(rows, value_key=value_key, actual_key=actual_key, alert_threshold=threshold)
    state = {"algorithm": rows[0]["algorithm"] if rows else None, "rows": len(rows), "scope": rows[0]["scope"] if rows else None}
    state_bytes = len(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "scope": rows[0]["scope"] if rows else None,
        "algorithm": rows[0]["algorithm"] if rows else None,
        "horizon_hours": rows[0]["horizon_hours"] if rows else None,
        "metrics": metrics,
        "time_quality": _time_quality(rows),
        "resource": {
            "cpu_time_ms": round((time.process_time() - started_cpu) * 1000, 6),
            "wall_time_ms": round((time.perf_counter() - started_wall) * 1000, 6),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "state_bytes": state_bytes,
        },
    }


def _load(session, query: str, params: dict, *, scope_sql: str, algorithm: str, value_key: str, actual_key: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in session.execute(text(query), params).mappings():
        item = dict(row)
        item["algorithm"] = algorithm
        item["scope"] = scope_sql(item)
        grouped[item["scope"]].append(item)
    return [rows for rows in grouped.values()]


def build_report() -> dict[str, object]:
    with SessionLocal() as session:
        node_groups = _load(
            session,
            """SELECT cluster_name, host, metric, horizon_hours, algorithm,
                      predicted_at, predicted_percent AS predicted_value,
                      actual_percent AS actual_value, coverage_ratio, max_gap_hours
               FROM node_resource_forecast_runs
               WHERE algorithm='linear' AND status='EVALUATED'
               ORDER BY predicted_at""",
            {}, scope_sql=lambda row: f"node|{row['cluster_name']}|{row['host']}|{row['metric']}|h{row['horizon_hours']}",
            algorithm="linear", value_key="predicted_value", actual_key="actual_value",
        )
        volume_groups = _load(
            session,
            """SELECT cluster_id, pool, image, metric, horizon_hours, algorithm,
                      predicted_at, predicted_value, actual_value
               FROM volume_forecast_runs
               WHERE algorithm='seasonal_median' AND status='EVALUATED'
               ORDER BY predicted_at""",
            {}, scope_sql=lambda row: f"volume|{row['cluster_id']}|{row['pool']}|{row['image']}|{row['metric']}|h{row['horizon_hours']}",
            algorithm="seasonal_median", value_key="predicted_value", actual_key="actual_value",
        )
    scopes = [
        _scope_report(rows, value_key="predicted_value", actual_key="actual_value", threshold=90.0)
        for rows in node_groups
    ]
    scopes.extend(
        _scope_report(rows, value_key="predicted_value", actual_key="actual_value", threshold=None)
        for rows in volume_groups
    )
    manifest = {
        "schema": "ceph-ai.forecast-baseline.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "active_baselines": {"NODE_RESOURCE": "linear", "VOLUME": "seasonal_median"},
        "scope_count": len(scopes),
        "scopes": scopes,
        "side_effects": "read-only; no database writes, alerts, notifications, remediation, promotion or model-state mutation",
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["artifact_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
