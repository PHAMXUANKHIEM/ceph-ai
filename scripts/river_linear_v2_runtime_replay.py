#!/usr/bin/env python3
"""Replay River Linear v2 against persisted runtime forecasts, read-only."""

from __future__ import annotations

import argparse
import json
import math
import resource
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from shared.db import SessionLocal
from shared.forecast_features import MetricPoint, build_features
from shared.models import NodeResourceForecastRun, OnlineLearnerLabel
from shared.river_linear_v2 import RiverLinearV2, VERIFIED_OUTCOMES


def _replay_scope(runs: list[NodeResourceForecastRun], labels: dict[str, OnlineLearnerLabel]) -> dict[str, object]:
    # Keep replay bounded and representative of the current runtime window.
    runs = sorted(runs, key=lambda row: row.predicted_at)[-512:]
    values = [MetricPoint(row.predicted_at, float(row.current_percent)) for row in runs]
    model: RiverLinearV2 | None = None
    scores: list[float] = []
    skipped_quality = skipped_schema = verified = 0
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    for index, row in enumerate(runs):
        feature_set = build_features(
            values[:index + 1], observed_at=row.predicted_at,
            expected_interval_seconds=3600.0, max_gap_seconds=7200.0,
            metric=row.metric, horizon_hours=row.horizon_hours,
        )
        if feature_set.quality_status != "OK":
            skipped_quality += 1
            continue
        if model is None:
            model = RiverLinearV2(
                feature_names=tuple(feature_set.features),
                feature_schema=feature_set.feature_schema,
            )
        if set(feature_set.features) != set(model.feature_names):
            skipped_schema += 1
            continue
        label = labels.get(row.id)
        if label is None or label.outcome not in VERIFIED_OUTCOMES:
            continue
        verified += 1
        if model.sample_count:
            prediction = model.predict_one(feature_set.features)
            if prediction is not None and math.isfinite(float(prediction)):
                scores.append(float(prediction) - float(label.label_value))
        model.learn_one(feature_set.features, label.label_value, outcome=label.outcome)
    return {
        "scope": f"{runs[0].cluster_name}|{runs[0].host}|{runs[0].metric}|h{runs[0].horizon_hours}",
        "algorithm": "river_linear_v2",
        "feature_schema": model.feature_schema if model else None,
        "verified_outcomes": verified,
        "scored_outcomes": len(scores),
        "mae": round(sum(abs(value) for value in scores) / len(scores), 6) if scores else None,
        "rmse": round(math.sqrt(sum(value * value for value in scores) / len(scores)), 6) if scores else None,
        "skipped_quality": skipped_quality,
        "skipped_schema": skipped_schema,
        "state_bytes": int(model.resource_usage()["state_bytes"]) if model else 0,
        "cpu_time_ms": round((time.process_time() - started_cpu) * 1000, 3),
        "wall_time_ms": round((time.perf_counter() - started_wall) * 1000, 3),
        "execution_mode": "SHADOW_ONLY",
    }


def build_report() -> dict[str, object]:
    with SessionLocal() as session:
        rows = session.query(NodeResourceForecastRun).filter(
            NodeResourceForecastRun.algorithm == "linear",
            NodeResourceForecastRun.status == "EVALUATED",
            NodeResourceForecastRun.actual_percent.isnot(None),
        ).all()
        run_ids = [row.id for row in rows]
        raw_labels = session.query(OnlineLearnerLabel).filter(
            OnlineLearnerLabel.source_run_id.in_(run_ids),
            OnlineLearnerLabel.status.in_(("READY", "CONSUMED")),
            OnlineLearnerLabel.outcome.in_(VERIFIED_OUTCOMES),
        ).all() if run_ids else []
    labels = {label.source_run_id: label for label in raw_labels}
    grouped: dict[tuple[str, str, str, int], list[NodeResourceForecastRun]] = defaultdict(list)
    for row in rows:
        grouped[(row.cluster_name, row.host, row.metric, row.horizon_hours)].append(row)
    scopes = [_replay_scope(scope_rows, labels) for scope_rows in grouped.values()]
    return {
        "schema": "ceph-ai.river-linear-v2-runtime-replay.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "execution_mode": "SHADOW_ONLY",
        "scope_count": len(scopes),
        "scopes": scopes,
        "side_effects": "read-only; no database writes, alerts, notifications, remediation, promotion or registry mutation",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({
        "scope_count": report["scope_count"],
        "scored_scopes": sum(bool(item["scored_outcomes"]) for item in report["scopes"]),
        "total_verified": sum(item["verified_outcomes"] for item in report["scopes"]),
        "total_scored": sum(item["scored_outcomes"] for item in report["scopes"]),
        "read_only": report["read_only"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
