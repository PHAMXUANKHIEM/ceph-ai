#!/usr/bin/env python3
"""Create an immutable, read-only baseline for self-learning forecasting.

The collector only executes SELECT statements.  It intentionally reports
``unavailable`` when an optional table is missing instead of guessing a zero.
The output file is created with O_EXCL and made read-only; rerunning against
the same path fails rather than replacing evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from sqlalchemy import inspect, text

from config.settings import settings
from shared.db import SessionLocal
from shared.forecast_flags import candidate_flags_snapshot


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _table_names(session) -> set[str]:
    try:
        return set(inspect(session.bind).get_table_names())
    except Exception:
        return set()


def _rows(session, tables: set[str], table: str, query: str, params: dict | None = None) -> list[dict]:
    if table not in tables:
        return []
    return [dict(row) for row in session.execute(text(query), params or {}).mappings()]


def _forecast_metrics(session, tables: set[str], table: str, *, node: bool) -> list[dict]:
    if table not in tables:
        return []
    scope = (
        "cluster_name, host, metric, horizon_hours, algorithm"
        if node else "cluster_id, pool, image, metric, horizon_hours, algorithm"
    )
    actual = "actual_percent" if node else "actual_value"
    predicted = "predicted_percent" if node else "predicted_value"
    error = f"({predicted} - {actual})"
    query = f"""
        SELECT {scope},
               COUNT(*) AS total,
               SUM(CASE WHEN status='EVALUATED' AND {actual} IS NOT NULL THEN 1 ELSE 0 END) AS evaluated,
               SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN status NOT IN ('EVALUATED', 'PENDING') THEN 1 ELSE 0 END) AS skipped,
               AVG(CASE WHEN status='EVALUATED' AND {actual} IS NOT NULL THEN ABS({error}) END) AS mae,
               SQRT(AVG(CASE WHEN status='EVALUATED' AND {actual} IS NOT NULL THEN {error} * {error} END)) AS rmse,
               AVG(CASE WHEN status='EVALUATED' AND {actual} IS NOT NULL
                   THEN CASE WHEN ABS({predicted}) + ABS({actual}) = 0 THEN 0
                   ELSE 200.0 * ABS({error}) / (ABS({predicted}) + ABS({actual})) END END) AS smape,
               AVG(CASE WHEN status='EVALUATED' AND {actual} IS NOT NULL THEN {error} END) AS bias
        FROM {table}
        GROUP BY {scope}
        ORDER BY {scope}
    """
    return _rows(session, tables, table, query)


def _sample_quality(session, tables: set[str]) -> list[dict]:
    if "online_learner_audit" not in tables:
        return [{"status": "UNAVAILABLE", "reason": "online_learner_audit table is missing"}]
    return _rows(session, tables, "online_learner_audit", """
        SELECT quality_status AS status, COUNT(*) AS samples,
               SUM(CASE WHEN update_applied THEN 1 ELSE 0 END) AS verified_outcome_updates
        FROM online_learner_audit
        GROUP BY quality_status ORDER BY quality_status
    """)


def _sample_intervals(session, tables: set[str]) -> list[dict]:
    if "online_learner_audit" not in tables:
        return [{"status": "UNAVAILABLE", "reason": "online_learner_audit table is missing"}]
    rows = _rows(session, tables, "online_learner_audit", """
        SELECT cluster_key, host, metric, observed_at
        FROM online_learner_audit
        ORDER BY cluster_key, host, metric, observed_at
        LIMIT 100000
    """)
    grouped: dict[tuple[str, str, str], list[datetime]] = defaultdict(list)
    for row in rows:
        value = row.get("observed_at")
        if isinstance(value, datetime):
            grouped[(str(row["cluster_key"]), str(row["host"]), str(row["metric"]))].append(value)
    result = []
    for scope, timestamps in sorted(grouped.items()):
        if len(timestamps) < 2:
            result.append({"scope": "|".join(scope), "samples": len(timestamps), "status": "INSUFFICIENT_SAMPLES"})
            continue
        intervals = [max(0.0, (right - left).total_seconds()) for left, right in zip(timestamps, timestamps[1:])]
        typical = median(intervals)
        span = max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())
        expected = max(len(timestamps), int(span / typical) + 1) if typical else len(timestamps)
        gaps = [value for value in intervals if value > float(settings.online_learning_sample_max_gap_seconds)]
        result.append({
            "scope": "|".join(scope),
            "samples": len(timestamps),
            "history_hours": round(span / 3600.0, 3),
            "median_interval_seconds": round(typical, 3),
            "max_gap_seconds": round(max(intervals), 3),
            "gap_count": len(gaps),
            "missing_rate": round(max(0.0, 1.0 - len(timestamps) / expected), 6),
            "status": "OK" if not gaps else "GAP_DETECTED",
        })
    return result or [{"status": "NO_SAMPLES"}]


def _count_by(session, tables: set[str], table: str, field: str) -> dict[str, int | str]:
    rows = _rows(session, tables, table, f"SELECT {field} AS key, COUNT(*) AS value FROM {table} GROUP BY {field}")
    return {str(row["key"]): int(row["value"] or 0) for row in rows}


def collect_baseline() -> dict[str, object]:
    started = time.perf_counter()
    cpu_started = time.process_time()
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with SessionLocal() as session:
        tables = _table_names(session)
        node = _forecast_metrics(session, tables, "node_resource_forecast_runs", node=True)
        volume = _forecast_metrics(session, tables, "volume_forecast_runs", node=False)
        alerts = _count_by(session, tables, "node_resource_forecast_alerts", "lifecycle_state")
        telegram = _count_by(session, tables, "telegram_outbox", "status")
        quality = _sample_quality(session, tables)
        sample_intervals = _sample_intervals(session, tables)
        learning = {
            "online_learner_states": len(_rows(session, tables, "online_learner_states", "SELECT id FROM online_learner_states")),
            "online_learner_audit": len(_rows(session, tables, "online_learner_audit", "SELECT id FROM online_learner_audit")),
            "log_learning_samples": len(_rows(session, tables, "log_learning_samples", "SELECT id FROM log_learning_samples")),
            "verified_log_outcomes": len(_rows(session, tables, "log_learning_samples", "SELECT id FROM log_learning_samples WHERE eligible_for_learning = true AND label NOT IN ('UNVERIFIED', 'INCONCLUSIVE')")),
        }
        false_positive = {
            "node_feedback": _count_by(session, tables, "node_resource_forecast_feedback", "verdict"),
            "known_false_positive_count": sum(
                value for key, value in _count_by(session, tables, "node_resource_forecast_feedback", "verdict").items()
                if key.lower().replace("-", "_") in {"false_positive", "fp"}
            ),
        }
        registry = {
            "models": _count_by(session, tables, "forecast_model_registry", "status"),
            "evaluations": len(_rows(session, tables, "forecast_model_evaluations", "SELECT id FROM forecast_model_evaluations")),
            "promotion_audits": len(_rows(session, tables, "forecast_model_promotion_audits", "SELECT id FROM forecast_model_promotion_audits")),
        }
        state_sizes = _rows(session, tables, "online_learner_states", "SELECT cluster_key, host, metric, LENGTH(state_json) AS state_bytes FROM online_learner_states")
        state_size = {
            "count": len(state_sizes),
            "total_bytes": sum(int(row.get("state_bytes") or 0) for row in state_sizes),
            "max_bytes": max((int(row.get("state_bytes") or 0) for row in state_sizes), default=0),
        }
    elapsed = time.perf_counter() - started
    cpu = time.process_time() - cpu_started
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "schema": "ceph-ai.self-learning-baseline.v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "read_only": True,
        "immutable": True,
        "feature_flags": candidate_flags_snapshot(),
        "runtime_flags": {
            "online_learning_enabled": bool(settings.online_learning_enabled),
            "online_learning_mode": settings.online_learning_mode,
            "online_learning_kill_switch": bool(settings.online_learning_kill_switch),
            "canary_enabled": bool(settings.online_learning_canary_enabled),
        },
        "scopes": {"node_resource": node, "volume": volume},
        "alerts": {"lifecycle": alerts, "volume": sum(alerts.values()) if alerts else 0},
        "telegram": {"outbox_status": telegram, "sent_or_pending": sum(telegram.values()) if telegram else 0},
        "false_positive": false_positive,
        "data_quality": quality,
        "samples": sample_intervals,
        "verified_outcomes": learning,
        "resources": {
            "wall_seconds": round(elapsed, 6),
            "cpu_seconds": round(cpu, 6),
            "rss_max_kib_delta": max(0, int(rss_after - rss_before)),
            "state_size": state_size,
        },
        "safety": {
            "candidate_side_effects": "NOT_EXECUTED_READ_ONLY_BASELINE",
            "restart_snapshot_registry_evaluation": "COVERED_BY_PHASE0_TESTS",
            "promotion_requires_operator_approval": True,
            "rollback_requires_operator_approval": True,
            "auto_remediation_enabled_by_baseline": False,
        },
    }


def render_markdown(report: dict[str, object]) -> str:
    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return (
        "# Self-learning baseline\n\n"
        f"- Generated at: `{report.get('generated_at', 'unknown')}`\n"
        f"- Git SHA: `{report.get('git_sha') or 'unknown'}`\n"
        f"- Report SHA-256: `{digest}`\n"
        "- Read-only collection: `true`\n"
        "- Immutable artifact: `true`\n\n"
        "The JSON payload below is the canonical baseline evidence. This file is "
        "created exclusively and must not be overwritten.\n\n"
        "```json\n" + json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n"
    )


def write_immutable_report(path: str | Path, report: dict[str, object]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = render_markdown(report).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(destination, flags, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    os.chmod(destination, 0o444)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    path = write_immutable_report(args.output, collect_baseline())
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
