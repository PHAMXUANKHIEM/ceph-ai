#!/usr/bin/env python3
"""Classify persisted forecast drift without changing runtime state."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from config.settings import settings
from shared.db import SessionLocal
from shared.models import NodeResourceForecastRun, NodeResourceModelState


def classify_drift(*, drift_reason: str | None, coverage_ratio: float | None,
                   max_gap_hours: float | None) -> tuple[str, str]:
    """Classify only what persisted quality evidence supports.

    A baseline/residual shift with adequate coverage is a workload/distribution
    change candidate, not proof of a root cause.  Missing or poor-quality
    telemetry remains a data-quality classification and must not be "fixed" by
    resetting drift.
    """
    min_coverage = float(settings.node_resource_forecast_min_coverage)
    max_gap = float(settings.node_resource_forecast_max_gap_hours)
    if (
        coverage_ratio is None or coverage_ratio < min_coverage
        or max_gap_hours is None or max_gap_hours > max_gap
    ):
        return "DATA_QUALITY_OR_COLLECTOR_GAP", "coverage or gap is outside the configured quality gate"
    reason = (drift_reason or "").lower()
    if "baseline shift" in reason or "residual shift" in reason:
        return "WORKLOAD_OR_DISTRIBUTION_CHANGE_CANDIDATE", (
            "baseline/residual shift with adequate coverage; root cause still requires operator review"
        )
    return "UNCLASSIFIED_DRIFT", "drift reason is not sufficient for a safe classification"


def build_report() -> dict[str, object]:
    rows: list[dict[str, object]] = []
    with SessionLocal() as session:
        states = session.query(NodeResourceModelState).filter_by(selected=True).all()
        for state in states:
            active_rows = session.query(NodeResourceForecastRun).filter_by(
                cluster_name=state.cluster_name, host=state.host, metric=state.metric,
                algorithm=state.algorithm, window_hours=state.window_hours,
                horizon_hours=state.horizon_hours, status="EVALUATED",
            ).filter(NodeResourceForecastRun.actual_percent.isnot(None)).all()
            active_targets = {row.target_at for row in active_rows}
            candidates = session.query(NodeResourceForecastRun).filter_by(
                cluster_name=state.cluster_name, host=state.host, metric=state.metric,
                horizon_hours=state.horizon_hours, status="EVALUATED",
            ).filter(NodeResourceForecastRun.actual_percent.isnot(None)).all()
            for algorithm, window_hours in sorted({
                (row.algorithm, row.window_hours) for row in candidates
            }):
                if algorithm == state.algorithm and window_hours == state.window_hours:
                    continue
                paired = [
                    row for row in candidates
                    if row.algorithm == algorithm
                    and row.window_hours == window_hours
                    and row.target_at in active_targets
                ]
                latest = max(paired, key=lambda row: row.target_at, default=None)
                if latest is None or latest.drift_status != "DRIFT":
                    continue
                classification, classification_reason = classify_drift(
                    drift_reason=latest.drift_reason,
                    coverage_ratio=latest.coverage_ratio,
                    max_gap_hours=latest.max_gap_hours,
                )
                rows.append({
                    "scope": f"{state.cluster_name}|{state.host}|{state.metric}|h{state.horizon_hours}",
                    "cluster": state.cluster_name,
                    "host": state.host,
                    "metric": state.metric,
                    "horizon_hours": state.horizon_hours,
                    "candidate_algorithm": algorithm,
                    "candidate_window_hours": window_hours,
                    "paired_evaluations": len(paired),
                    "drift_score": latest.drift_score,
                    "drift_reason": latest.drift_reason,
                    "coverage_ratio": latest.coverage_ratio,
                    "max_gap_hours": latest.max_gap_hours,
                    "latest_target_at": latest.target_at,
                    "classification": classification,
                    "classification_reason": classification_reason,
                    "decision": "HOLD",
                })
    return {
        "schema": "ceph-ai.forecast-drift-scope-classification.v1",
        "read_only": True,
        "comparison_count": len(rows),
        "logical_scope_count": len({row["scope"] for row in rows}),
        "classification_counts": dict(Counter(row["classification"] for row in rows)),
        "scopes": rows,
        "side_effects": "read-only; no drift reset, threshold change, promotion, alert or remediation",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report()
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "comparison_count": report["comparison_count"],
        "logical_scope_count": report["logical_scope_count"],
        "classification_counts": report["classification_counts"],
        "read_only": report["read_only"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
