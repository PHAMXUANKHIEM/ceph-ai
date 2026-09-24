#!/usr/bin/env python3
"""Replay River Linear v2 against persisted runtime forecasts, read-only."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings
from shared.db import SessionLocal
from shared.forecast_features import MetricPoint, build_features
from shared.models import NodeResourceForecastRun, OnlineLearnerLabel
from shared.river_linear_v2 import RiverLinearV2, VERIFIED_OUTCOMES
from watcher.node_resource_forecast import fetch_samples


def _continuous_history(
    points: list[MetricPoint], cutoff: datetime, *, max_gap_seconds: float,
) -> list[MetricPoint]:
    """Return the latest contiguous telemetry segment before ``cutoff``.

    A recovered collector must not make River learn across an outage.  The
    previous implementation used forecast rows as observations, so a gap in
    the forecast scheduler was incorrectly treated as a telemetry gap.  This
    helper operates on the real Loki samples and starts a new history after
    the latest material outage.
    """
    cutoff_utc = cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=timezone.utc)
    ordered = sorted(
        (point for point in points if point.observed_at <= cutoff_utc),
        key=lambda point: point.observed_at,
    )
    if not ordered:
        return []
    boundary = 0
    for index in range(len(ordered) - 1, 0, -1):
        gap = (ordered[index].observed_at - ordered[index - 1].observed_at).total_seconds()
        if gap > max_gap_seconds:
            boundary = index
            break
    # Features only need a bounded recent history.  Keeping the bound here
    # also prevents a 30-day Loki history from making replay quadratic.
    return ordered[boundary:][-512:]


def _replay_scope(
    runs: list[NodeResourceForecastRun],
    labels: dict[str, OnlineLearnerLabel],
    observations: list[MetricPoint] | None = None,
) -> dict[str, object]:
    # Keep replay bounded and representative of the current runtime window.
    runs = sorted(runs, key=lambda row: row.predicted_at)[-512:]
    using_runtime_telemetry = observations is not None
    fallback_values = [MetricPoint(row.predicted_at, float(row.current_percent)) for row in runs]
    values = observations if using_runtime_telemetry else fallback_values
    expected_interval_seconds = max(60.0, float(settings.node_health_scan_interval_seconds))
    max_gap_seconds = max(
        expected_interval_seconds * 2,
        float(settings.node_resource_forecast_max_gap_hours) * 3600.0,
    )
    model: RiverLinearV2 | None = None
    scores: list[float] = []
    baseline_scores: list[float] = []
    naive_scores: list[float] = []
    skipped_quality = skipped_schema = verified = 0
    quality_blockers: dict[str, int] = {}
    latest_evidence_at: datetime | None = None
    pending: list[tuple[datetime, dict[str, float], OnlineLearnerLabel]] = []
    started_wall = time.perf_counter()
    started_cpu = time.process_time()
    for index, row in enumerate(runs):
        # Only labels verified BEFORE this prediction can update the model.
        # A later outcome may score this prediction, but cannot train it.
        due = [item for item in pending if item[0] <= row.predicted_at]
        pending = [item for item in pending if item[0] > row.predicted_at]
        for _verified_at, features, ready_label in due:
            if model is not None:
                model.learn_one(features, ready_label.label_value, outcome=ready_label.outcome)
        history = _continuous_history(
            values, row.predicted_at, max_gap_seconds=max_gap_seconds,
        ) if using_runtime_telemetry else values[:index + 1]
        feature_set = build_features(
            history, observed_at=row.predicted_at,
            expected_interval_seconds=expected_interval_seconds,
            max_gap_seconds=max_gap_seconds,
            metric=row.metric, horizon_hours=row.horizon_hours,
        )
        if using_runtime_telemetry and history and (
            row.predicted_at.replace(tzinfo=timezone.utc) - history[-1].observed_at
        ).total_seconds() > max_gap_seconds:
            feature_set = replace(feature_set, quality_status="GAP_DETECTED")
        if feature_set.quality_status != "OK":
            skipped_quality += 1
            quality_blockers[feature_set.quality_status] = quality_blockers.get(feature_set.quality_status, 0) + 1
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
        if (
            label is None or label.outcome not in VERIFIED_OUTCOMES
            or label.source_actor != "forecast-evaluator"
            or not label.evidence_fingerprint
            or row.actual_percent is None
            or not math.isfinite(float(label.label_value))
            or abs(float(label.label_value) - float(row.actual_percent)) > 0.01
            or label.verified_at is None
            or label.verified_at < row.target_at
        ):
            continue
        verified += 1
        latest_evidence_at = max(latest_evidence_at, label.verified_at) if latest_evidence_at else label.verified_at
        baseline_prediction = getattr(row, "predicted_percent", None)
        if baseline_prediction is not None and math.isfinite(float(baseline_prediction)):
            baseline_scores.append(float(baseline_prediction) - float(label.label_value))
        naive_prediction = feature_set.features.get("current")
        if naive_prediction is not None and math.isfinite(float(naive_prediction)):
            naive_scores.append(float(naive_prediction) - float(label.label_value))
        if model.sample_count:
            prediction = model.predict_one(feature_set.features)
            if prediction is not None and math.isfinite(float(prediction)):
                scores.append(float(prediction) - float(label.label_value))
        pending.append((label.verified_at, feature_set.features, label))
    status = (
        "TELEMETRY_UNAVAILABLE" if using_runtime_telemetry and not values else
        "DATA_QUALITY_BLOCKED" if skipped_quality and verified == 0 else
        "MODEL_NOT_RUN" if model is None else
        "NO_VERIFIED_OUTCOME" if verified == 0 else
        "INSUFFICIENT_SAMPLE" if not scores else "SCORED"
    )
    return {
        "scope": f"{runs[0].cluster_name}|{runs[0].host}|{runs[0].metric}|h{runs[0].horizon_hours}",
        "algorithm": "river_linear_v2",
        "feature_schema": model.feature_schema if model else None,
        "verified_outcomes": verified,
        "scored_outcomes": len(scores),
        "evidence_status": status,
        "promotion_blocked_reason": "River v2 remains SHADOW_ONLY; operator approval and holdout evidence are required",
        "latest_evidence_at": latest_evidence_at.isoformat() if latest_evidence_at else None,
        "active_model": "linear",
        "shadow_model": "river_linear_v2",
        "quality_gap_count": skipped_quality + skipped_schema,
        "mae": round(sum(abs(value) for value in scores) / len(scores), 6) if scores else None,
        "rmse": round(math.sqrt(sum(value * value for value in scores) / len(scores)), 6) if scores else None,
        "baseline_algorithm": "persisted_linear_forecast",
        "baseline_scored_outcomes": len(baseline_scores),
        "baseline_mae": round(sum(abs(value) for value in baseline_scores) / len(baseline_scores), 6) if baseline_scores else None,
        "baseline_rmse": round(math.sqrt(sum(value * value for value in baseline_scores) / len(baseline_scores)), 6) if baseline_scores else None,
        "naive_baseline": "last_observed_value",
        "naive_scored_outcomes": len(naive_scores),
        "naive_mae": round(sum(abs(value) for value in naive_scores) / len(naive_scores), 6) if naive_scores else None,
        "naive_rmse": round(math.sqrt(sum(value * value for value in naive_scores) / len(naive_scores)), 6) if naive_scores else None,
        "skipped_quality": skipped_quality,
        "quality_blockers": quality_blockers,
        "telemetry_source": "loki" if using_runtime_telemetry else "forecast-fallback-for-tests",
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
            OnlineLearnerLabel.source_actor == "forecast-evaluator",
            OnlineLearnerLabel.evidence_fingerprint.isnot(None),
        ).all() if run_ids else []
    labels = {label.source_run_id: label for label in raw_labels}
    grouped: dict[tuple[str, str, str, int], list[NodeResourceForecastRun]] = defaultdict(list)
    for row in rows:
        grouped[(row.cluster_name, row.host, row.metric, row.horizon_hours)].append(row)
    telemetry_cache: dict[tuple[str, str], list[tuple[datetime, float, float]]] = {}
    scopes = []
    for (cluster, host, metric, _horizon), scope_rows in grouped.items():
        cache_key = (cluster, host)
        if cache_key not in telemetry_cache:
            try:
                raw_samples = fetch_samples(cluster, host)
                telemetry_cache[cache_key] = raw_samples
            except Exception:
                telemetry_cache[cache_key] = []
        scopes.append(_replay_scope(
            scope_rows,
            labels,
            [
                MetricPoint(timestamp, cpu if metric == "cpu" else memory)
                for timestamp, cpu, memory in telemetry_cache[cache_key]
            ],
        ))
    report_status = (
        "NO_DATA" if not scopes else
        "SCORED" if any(item["scored_outcomes"] for item in scopes) else
        "TELEMETRY_UNAVAILABLE" if any(item["evidence_status"] == "TELEMETRY_UNAVAILABLE" for item in scopes) else
        "DATA_QUALITY_BLOCKED" if any(item["quality_gap_count"] for item in scopes) else
        "NO_VERIFIED_OUTCOME" if all(item["verified_outcomes"] == 0 for item in scopes) else
        "INSUFFICIENT_SAMPLE"
    )
    return {
        "schema": "ceph-ai.river-linear-v2-runtime-replay.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "execution_mode": "SHADOW_ONLY",
        "scope_count": len(scopes),
        "evidence_status": report_status,
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
