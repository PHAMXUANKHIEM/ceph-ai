"""Background-only SNARIMAX shadow scan over persisted host telemetry.

The scan reads ``HostMetricSample`` locally, aggregates to complete hourly
buckets, and writes only auditable SHADOW evidence/state.  It never calls
Ceph/SSH, alert synchronization, Telegram, remediation or promotion code.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from statistics import fmean

from config.settings import settings
from shared import db
from shared.models import Cluster, HostMetricSample, NodeResourceForecastRun, OnlineLearnerState
from shared.river_snarimax import (
    ALGORITHM,
    FEATURE_SCHEMA,
    MODEL_VERSION,
    SNARIMAX_PROFILES,
    SnarimaxShadowRunner,
    snapshot_checksum,
)

logger = logging.getLogger(__name__)

_RUNNER = SnarimaxShadowRunner(
    failure_threshold=settings.snarimax_shadow_circuit_breaker_failures,
    cooldown_seconds=settings.snarimax_shadow_circuit_breaker_cooldown_seconds,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _metric_value(row: HostMetricSample, metric: str) -> float:
    if metric == "cpu":
        return float(row.cpu_percent)
    if metric == "ram":
        return float(row.mem_percent)
    return float(row.disk_read_iops or 0.0) + float(row.disk_write_iops or 0.0)


def hourly_points(
    rows: list[HostMetricSample],
    metric: str,
    *,
    now: datetime,
) -> list[tuple[datetime, float]]:
    """Aggregate complete hourly buckets and exclude the current partial hour."""
    current_hour = _utc(now).replace(minute=0, second=0, microsecond=0)
    buckets: dict[datetime, list[float]] = {}
    for row in rows:
        observed_at = _utc(row.collected_at)
        bucket = observed_at.replace(minute=0, second=0, microsecond=0)
        if bucket >= current_hour:
            continue
        try:
            value = _metric_value(row, metric)
            if value != value or value in (float("inf"), float("-inf")):
                continue
        except (TypeError, ValueError):
            continue
        buckets.setdefault(bucket, []).append(value)
    return [(bucket, fmean(values)) for bucket, values in sorted(buckets.items())]


def _state_snapshot(session, cluster_key: str, host: str, metric: str):
    row = session.query(OnlineLearnerState).filter_by(
        cluster_key=cluster_key,
        host=host,
        metric=metric,
        model_version=MODEL_VERSION,
    ).one_or_none()
    if row is None:
        return None
    try:
        payload = json.loads(row.state_json)
        if not isinstance(payload, dict) or row.state_checksum != snapshot_checksum(payload):
            raise ValueError("SNARIMAX persisted state checksum mismatch")
        return payload
    except (TypeError, ValueError, json.JSONDecodeError, OverflowError) as exc:
        # Passing the corrupt payload to the runner makes the failure explicit
        # and returns the active baseline for this scan. Do not silently train
        # a fresh candidate over a potentially unsafe state.
        logger.warning("snarimax shadow state rejected for %s/%s: %s", host, metric, exc)
        return {"_corrupt": True, "reason": str(exc)}


def _save_state(session, cluster_key: str, host: str, metric: str, result) -> None:
    if not result.snapshot:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = session.query(OnlineLearnerState).filter_by(
        cluster_key=cluster_key, host=host, metric=metric, model_version=MODEL_VERSION,
    ).one_or_none()
    payload = result.snapshot
    values = {
        "cluster_key": cluster_key,
        "host": host,
        "metric": metric,
        "model_version": MODEL_VERSION,
        "algorithm": ALGORITHM,
        "feature_schema": FEATURE_SCHEMA,
        "state_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "state_checksum": snapshot_checksum(payload),
        "sample_count": int(result.sample_count),
        "last_learned_at": result.observed_at.replace(tzinfo=None) if result.observed_at else None,
        "updated_at": now,
    }
    if row is None:
        session.add(OnlineLearnerState(created_at=now, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)


def _save_evidence(session, cluster_name: str, host: str, result) -> None:
    if result.status != "SHADOW_ONLY" or result.prediction is None or result.observed_at is None:
        return
    predicted_at = result.observed_at.replace(tzinfo=None)
    target_at = predicted_at + timedelta(hours=1)
    bucket = int(result.observed_at.timestamp())
    key = f"{cluster_name}|{host}|{result.metric}|{ALGORITHM}|{bucket}"
    if session.query(NodeResourceForecastRun.id).filter_by(idempotency_key=key).first():
        return
    evidence = result.as_dict(include_snapshot=False)
    session.add(NodeResourceForecastRun(
        cluster_name=cluster_name,
        host=host,
        metric=result.metric,
        algorithm=ALGORITHM,
        window_hours=24,
        horizon_hours=1,
        predicted_at=predicted_at,
        target_at=target_at,
        current_percent=float(result.current_value if result.current_value is not None else result.prediction),
        predicted_percent=float(result.prediction),
        confidence=float(result.confidence),
        coverage_ratio=float(result.coverage_ratio),
        max_gap_hours=float(result.max_gap_seconds / 3600),
        latest_observed_at=predicted_at,
        consensus_status="SHADOW_ONLY",
        consensus_ratio=None,
        consensus_candidate_count=1,
        predicted_low=result.predicted_low,
        predicted_high=result.predicted_high,
        residual_percent=None,
        anomaly_score=None,
        model_votes_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        drift_status="SHADOW_ONLY",
        drift_score=0.0,
        drift_reason="SNARIMAX shadow evidence only",
        status="SHADOW",
        idempotency_key=key,
    ))


def run_cluster_shadow(cluster_id: str, cluster_name: str, *, now: datetime | None = None) -> dict:
    """Run one bounded background shadow scan for a cluster."""
    if not settings.snarimax_shadow_enabled:
        return {"status": "DISABLED", "processed": 0, "reports": []}
    started = time.monotonic()
    cpu_started = time.process_time()
    reference = _utc(now or datetime.now(timezone.utc))
    # The default watcher historically called auxiliary collectors without a
    # cluster id. Resolve that sentinel here so the shadow scan still reads
    # the real default-cluster rows instead of silently returning no data.
    requested_cluster_id = cluster_id
    reports = []
    processed = 0
    with db.SessionLocal() as session:
        if not requested_cluster_id or requested_cluster_id == "__default__":
            default_cluster = session.query(Cluster.id).filter(
                Cluster.is_default.is_(True),
            ).one_or_none()
            effective_cluster_id = default_cluster[0] if default_cluster else requested_cluster_id
        else:
            effective_cluster_id = requested_cluster_id
        cluster_key = str(effective_cluster_id or "__default__")
        hosts = [host for (host,) in session.query(HostMetricSample.host).filter(
            HostMetricSample.cluster_id == effective_cluster_id,
        ).distinct().limit(settings.snarimax_shadow_max_hosts).all()]
        processed_hosts = 0
        for host in hosts:
            if processed_hosts >= settings.snarimax_shadow_max_hosts:
                break
            processed_hosts += 1
            if time.monotonic() - started >= settings.snarimax_shadow_timeout_seconds:
                break
            rows = session.query(HostMetricSample).filter(
                HostMetricSample.cluster_id == effective_cluster_id,
                HostMetricSample.host == host,
                HostMetricSample.collected_at >= reference.replace(tzinfo=None)
                - timedelta(days=settings.node_resource_forecast_history_days),
            ).order_by(HostMetricSample.collected_at.desc()).limit(
                settings.snarimax_shadow_max_samples * 24,
            ).all()
            rows.reverse()
            for metric in SNARIMAX_PROFILES:
                if time.process_time() - cpu_started >= settings.snarimax_shadow_cpu_budget_seconds:
                    break
                points = hourly_points(rows, metric, now=reference)
                fallback = points[-1][1] if points else None
                snapshot = _state_snapshot(session, cluster_key, host, metric)
                result = _RUNNER.run(
                    f"{cluster_key}|{host}|{metric}", metric, points,
                    fallback=fallback,
                    timeout_seconds=settings.snarimax_shadow_model_timeout_seconds,
                    snapshot=snapshot,
                )
                reports.append({"host": host, **result.as_dict()})
                processed += 1
                if result.status == "SHADOW_ONLY":
                    _save_state(session, cluster_key, host, metric, result)
                    _save_evidence(session, cluster_name, host, result)
        session.commit()
    return {
        "status": "COMPLETED",
        "processed": processed,
        "reports": reports,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "cpu_time_ms": round((time.process_time() - cpu_started) * 1000, 3),
    }
