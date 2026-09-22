#!/usr/bin/env python3
"""Read-only 14-day persisted forecast replay evidence."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from shared.db import SessionLocal


def _node_evidence(session) -> dict[str, object]:
    total = ready = 0
    states = session.execute(text(
        "SELECT cluster_name, host, metric, algorithm, window_hours "
        "FROM node_resource_model_states WHERE selected = true"
    )).mappings()
    for state in states:
        active_targets = {
            row["target_at"] for row in session.execute(text(
                "SELECT target_at FROM node_resource_forecast_runs "
                "WHERE cluster_name=:cluster AND host=:host AND metric=:metric "
                "AND algorithm=:algorithm AND window_hours=:window "
                "AND status='EVALUATED' AND actual_percent IS NOT NULL"
            ), {
                "cluster": state["cluster_name"], "host": state["host"],
                "metric": state["metric"], "algorithm": state["algorithm"],
                "window": state["window_hours"],
            }).mappings()
        }
        candidates = list(session.execute(text(
            "SELECT algorithm, window_hours, target_at FROM node_resource_forecast_runs "
            "WHERE cluster_name=:cluster AND host=:host AND metric=:metric "
            "AND status='EVALUATED' AND actual_percent IS NOT NULL"
        ), {
            "cluster": state["cluster_name"], "host": state["host"], "metric": state["metric"],
        }).mappings())
        for algorithm, window in sorted({(row["algorithm"], row["window_hours"]) for row in candidates}):
            if (algorithm, window) == (state["algorithm"], state["window_hours"]):
                continue
            paired = [row for row in candidates
                      if (row["algorithm"], row["window_hours"]) == (algorithm, window)
                      and row["target_at"] in active_targets]
            if not paired:
                continue
            total += 1
            span_days = (max(row["target_at"] for row in paired) - min(row["target_at"] for row in paired)).total_seconds() / 86400
            if len(paired) >= 20 and span_days >= 14:
                ready += 1
    return {"comparisons": total, "ready_14d": ready}


def _volume_evidence(session) -> dict[str, object]:
    """Count paired volume candidates without selecting or mutating state."""
    total = ready = 0
    states = session.execute(text(
        "SELECT cluster_id, pool, image, metric, algorithm, window_hours, horizon_hours "
        "FROM volume_model_states WHERE selected = true"
    )).mappings()
    for state in states:
        active_targets = {
            row["target_at"] for row in session.execute(text(
                "SELECT target_at FROM volume_forecast_runs "
                "WHERE cluster_id=:cluster_id AND pool=:pool AND image=:image "
                "AND metric=:metric AND algorithm=:algorithm AND window_hours=:window "
                "AND horizon_hours=:horizon AND status='EVALUATED' "
                "AND actual_value IS NOT NULL"
            ), {
                "cluster_id": state["cluster_id"], "pool": state["pool"],
                "image": state["image"], "metric": state["metric"],
                "algorithm": state["algorithm"], "window": state["window_hours"],
                "horizon": state["horizon_hours"],
            }).mappings()
        }
        candidates = list(session.execute(text(
            "SELECT algorithm, window_hours, target_at FROM volume_forecast_runs "
            "WHERE cluster_id=:cluster_id AND pool=:pool AND image=:image "
            "AND metric=:metric AND horizon_hours=:horizon "
            "AND status='EVALUATED' AND actual_value IS NOT NULL"
        ), {
            "cluster_id": state["cluster_id"], "pool": state["pool"],
            "image": state["image"], "metric": state["metric"],
            "horizon": state["horizon_hours"],
        }).mappings())
        for algorithm, window in sorted({
            (row["algorithm"], row["window_hours"]) for row in candidates
        }):
            if (algorithm, window) == (state["algorithm"], state["window_hours"]):
                continue
            paired = [row for row in candidates
                      if (row["algorithm"], row["window_hours"]) == (algorithm, window)
                      and row["target_at"] in active_targets]
            if not paired:
                continue
            total += 1
            span_days = (
                max(row["target_at"] for row in paired)
                - min(row["target_at"] for row in paired)
            ).total_seconds() / 86400
            if len(paired) >= 20 and span_days >= 14:
                ready += 1
    return {"comparisons": total, "ready_14d": ready}


def build_report() -> dict[str, object]:
    with SessionLocal() as session:
        try:
            node = _node_evidence(session)
        except Exception as exc:
            node = {"comparisons": 0, "ready_14d": 0, "status": "BLOCKED_MIGRATION_PENDING", "reason": str(exc)[:240]}
        volume_columns = {column["name"] for column in inspect(session.bind).get_columns("volume_model_states")}
        volume_run_columns = {column["name"] for column in inspect(session.bind).get_columns("volume_forecast_runs")}
        if "horizon_hours" not in volume_columns or "horizon_hours" not in volume_run_columns:
            volume = {
                "comparisons": 0,
                "ready_14d": 0,
                "status": "BLOCKED_MIGRATION_PENDING",
                "reason": "volume forecast horizon_hours is missing",
            }
        else:
            try:
                volume = {**_volume_evidence(session), "status": "REPLAYED", "reason": None}
            except Exception as exc:
                volume = {
                    "comparisons": 0,
                    "ready_14d": 0,
                    "status": "BLOCKED_REPLAY_ERROR",
                    "reason": str(exc)[:240],
                }
    return {
        "schema": "ceph-ai.acceptance-replay.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "node_resource": node,
        "volume": volume,
        "status": "PASS" if (
            node["ready_14d"] > 0
            and volume["ready_14d"] > 0
            and volume["status"] == "REPLAYED"
        ) else "PARTIAL",
        "read_only": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = build_report()
    with open(args.output, "x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
