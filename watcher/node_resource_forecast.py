"""Loki-backed CPU/RAM monitoring and deterministic resource forecasting.

Loki is the source of truth. Alloy publishes the node-resource stream and
both current threshold monitoring and forecasting read that same stream;
the Watcher does not SSH to nodes to manufacture CPU/RAM observations.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import db
from shared.models import NodeResourceForecastRun, NodeResourceModelState

logger = logging.getLogger(__name__)
JOB = "ceph-ai-node-metrics"


@dataclass(frozen=True)
class ResourceForecast:
    metric: str
    current_percent: float
    slope_percent_per_hour: float
    predicted_percent: float
    hours_to_90: float | None
    confidence: float
    samples: int
    window_hours: float
    algorithm: str = "linear"
    training_window_hours: int | None = None


class NodeResourceLokiError(Exception):
    """The current CPU/RAM observation is absent, stale, or unreadable."""


def _headers() -> dict[str, str]:
    return ({"X-Scope-OrgID": settings.log_intel_loki_tenant}
            if settings.log_intel_loki_tenant else {})


def _base_url() -> str:
    return (settings.log_intel_loki_url or "").rstrip("/")


def push_sample(cluster: str, host: str, metrics: dict, *, timestamp_ns: int | None = None) -> bool:
    """Push one structured sample to Loki.  Never breaks node monitoring."""
    if not settings.node_resource_forecast_enabled or not _base_url():
        return False
    import httpx

    line = json.dumps({
        "cpu_percent": float(metrics["cpu_percent"]),
        "mem_percent": float(metrics["mem_percent"]),
        "mem_used_mb": float(metrics.get("mem_used_mb") or 0),
        "mem_total_mb": float(metrics.get("mem_total_mb") or 0),
    }, separators=(",", ":"))
    payload = {"streams": [{"stream": {
        "job": JOB, "cluster": cluster or "default", "host": host,
        "metric_type": "node_resource",
    }, "values": [[str(timestamp_ns or time.time_ns()), line]]}]}
    try:
        response = httpx.post(f"{_base_url()}/loki/api/v1/push", json=payload,
                              headers=_headers(), timeout=settings.log_intel_loki_timeout_seconds)
        response.raise_for_status()
        return True
    except Exception:
        logger.warning("node forecast: cannot push sample for %s to Loki", host, exc_info=True)
        return False


def fetch_samples(cluster: str, host: str, *, now: datetime | None = None) -> list[tuple[datetime, float, float]]:
    """Read CPU/RAM samples from Loki, ordered and deduplicated by timestamp."""
    if not _base_url():
        return []
    import httpx

    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(days=max(1, settings.node_resource_forecast_history_days))
    selector = '{job="%s", cluster="%s", host="%s"}' % (
        JOB, cluster.replace('"', '\\"'), host.replace('"', '\\"'))
    params = {"query": selector, "start": str(int(start.timestamp() * 1e9)),
              "end": str(int(end.timestamp() * 1e9)), "limit": "5000", "direction": "forward"}
    response = httpx.get(f"{_base_url()}/loki/api/v1/query_range", params=params,
                         headers=_headers(), timeout=settings.log_intel_loki_timeout_seconds)
    response.raise_for_status()
    rows: dict[int, tuple[datetime, float, float]] = {}
    for stream in ((response.json().get("data") or {}).get("result") or []):
        for ts_ns, line in stream.get("values") or []:
            try:
                raw = json.loads(line)
                ts_int = int(ts_ns)
                rows[ts_int] = (datetime.fromtimestamp(ts_int / 1e9, timezone.utc),
                                float(raw["cpu_percent"]), float(raw["mem_percent"]))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
    return [rows[key] for key in sorted(rows)]


def fetch_latest_metrics(
    cluster: str, host: str, *, now: datetime | None = None, max_age_seconds: int | None = None
) -> dict:
    """Return the newest CPU/RAM sample shipped by Alloy to Loki.

    Reject stale data so an interrupted Alloy/Loki path cannot keep an old
    high value open forever or falsely report that a node is healthy.
    """
    samples = fetch_samples(cluster, host, now=now)
    if not samples:
        raise NodeResourceLokiError(f"{host}: Loki has no CPU/RAM samples")
    observed_at, cpu, mem = samples[-1]
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    allowed_age = max_age_seconds or max(120, settings.node_health_scan_interval_seconds * 2)
    age_seconds = (reference - observed_at).total_seconds()
    if age_seconds > allowed_age:
        raise NodeResourceLokiError(
            f"{host}: latest Loki CPU/RAM sample is stale ({int(age_seconds)}s old)"
        )
    return {
        "cpu_percent": cpu,
        "mem_percent": mem,
        "observed_at": observed_at.isoformat(),
        "source": "loki",
    }


def _linear_forecast(
    points: list[tuple[datetime, float]], metric: str, *, horizon_hours: int | None = None,
    training_window_hours: int | None = None,
) -> ResourceForecast | None:
    minimum = max(3, settings.node_resource_forecast_min_samples)
    if len(points) < minimum:
        return None
    origin = points[0][0]
    xs = [(ts - origin).total_seconds() / 3600 for ts, _ in points]
    ys = [value for _, value in points]
    window = xs[-1] - xs[0]
    if window < 6:
        return None
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator <= 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    intercept = y_mean - slope * x_mean
    fitted = [intercept + slope * x for x in xs]
    ss_total = sum((y - y_mean) ** 2 for y in ys)
    ss_residual = sum((y - fit) ** 2 for y, fit in zip(ys, fitted))
    confidence = max(0.0, min(1.0, 1 - ss_residual / ss_total)) if ss_total > 0 else 0.0
    horizon = max(1, horizon_hours or settings.node_resource_forecast_horizon_hours)
    predicted = max(0.0, min(100.0, intercept + slope * (xs[-1] + horizon)))
    hours_to_90 = None
    if slope > 0 and ys[-1] < 90:
        crossing = (90 - intercept) / slope
        if crossing >= xs[-1]:
            hours_to_90 = crossing - xs[-1]
    return ResourceForecast(metric, ys[-1], slope, predicted, hours_to_90,
                            confidence, len(points), window,
                            training_window_hours=training_window_hours)


def _candidate_windows() -> list[int]:
    values: set[int] = set()
    for raw in settings.node_resource_learning_candidate_hours.split(","):
        try:
            value = int(raw.strip())
        except ValueError:
            continue
        if value >= 6:
            values.add(value)
    return sorted(values) or [24, 72, 168, 720]


def _window_points(
    points: list[tuple[datetime, float]], window_hours: int
) -> list[tuple[datetime, float]]:
    if not points:
        return []
    cutoff = points[-1][0] - timedelta(hours=window_hours)
    return [point for point in points if point[0] >= cutoff]


def _state_for(session, cluster: str, host: str, metric: str, window_hours: int):
    state = session.query(NodeResourceModelState).filter_by(
        cluster_name=cluster, host=host, metric=metric,
        algorithm="linear", window_hours=window_hours,
    ).one_or_none()
    if state is None:
        state = NodeResourceModelState(
            cluster_name=cluster, host=host, metric=metric,
            algorithm="linear", window_hours=window_hours,
        )
        session.add(state)
        session.flush()
    return state


def _evaluate_due(session, cluster: str, host: str, metric: str,
                  actual_percent: float, now_naive: datetime) -> None:
    due = session.query(NodeResourceForecastRun).filter_by(
        cluster_name=cluster, host=host, metric=metric, status="PENDING"
    ).filter(NodeResourceForecastRun.target_at <= now_naive).all()
    for run in due:
        error = abs(run.predicted_percent - actual_percent)
        run.actual_percent = actual_percent
        run.absolute_error = error
        run.status = "EVALUATED"
        run.evaluated_at = now_naive
        state = _state_for(session, cluster, host, metric, run.window_hours)
        old_count = state.evaluated_count
        old_mae = state.mean_absolute_error or 0.0
        state.evaluated_count = old_count + 1
        state.mean_absolute_error = (old_mae * old_count + error) / state.evaluated_count
        state.last_absolute_error = error


def evaluate_due_outcomes(
    cluster: str, host: str, cpu_percent: float, mem_percent: float,
    *, observed_at: datetime | None = None,
) -> int:
    """Score overdue forecasts from a fresh trusted observation.

    This does not require Loki query visibility, so an SSH fallback sample
    can immediately unblock online learning while that same sample is being
    pushed back into Loki for future history windows.
    """
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    with db.SessionLocal() as session:
        before = session.query(NodeResourceForecastRun).filter_by(
            cluster_name=cluster, host=host, status="PENDING"
        ).filter(NodeResourceForecastRun.target_at <= now).count()
        _evaluate_due(session, cluster, host, "cpu", float(cpu_percent), now)
        _evaluate_due(session, cluster, host, "ram", float(mem_percent), now)
        session.commit()
        return before


def _selected_window(session, cluster: str, host: str, metric: str,
                     available: list[int]) -> int:
    states = session.query(NodeResourceModelState).filter_by(
        cluster_name=cluster, host=host, metric=metric, algorithm="linear"
    ).all()
    eligible = [state for state in states
                if state.window_hours in available
                and state.evaluated_count >= settings.node_resource_learning_min_outcomes
                and state.mean_absolute_error is not None]
    selected = min(eligible, key=lambda state: state.mean_absolute_error).window_hours if eligible else max(available)
    for window in available:
        state = _state_for(session, cluster, host, metric, window)
        state.selected = window == selected
    return selected


def _record_candidates(session, cluster: str, host: str, metric: str,
                       candidates: dict[int, ResourceForecast], now_naive: datetime) -> None:
    horizon = max(1, settings.node_resource_learning_evaluation_hours)
    bucket = now_naive.replace(minute=0, second=0, microsecond=0)
    for window, prediction in candidates.items():
        key = f"{cluster}|{host}|{metric}|linear|{window}|{bucket.isoformat()}"
        exists = session.query(NodeResourceForecastRun.id).filter_by(idempotency_key=key).first()
        if exists:
            continue
        session.add(NodeResourceForecastRun(
            cluster_name=cluster, host=host, metric=metric, algorithm="linear",
            window_hours=window, predicted_at=now_naive,
            target_at=now_naive + timedelta(hours=horizon),
            current_percent=prediction.current_percent,
            predicted_percent=prediction.predicted_percent,
            confidence=prediction.confidence, status="PENDING", idempotency_key=key,
        ))


def adaptive_forecast(
    cluster: str, host: str, *, now: datetime | None = None
) -> dict[str, ResourceForecast]:
    """Evaluate old forecasts and select the lowest-MAE window per metric.

    Raw observations always come from Loki. PostgreSQL stores only forecast
    metadata/outcomes and the small online score state.
    """
    samples = fetch_samples(cluster, host, now=now)
    if not samples:
        return {}
    observed_at = now or samples[-1][0]
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    now_naive = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    result: dict[str, ResourceForecast] = {}
    with db.SessionLocal() as session:
        for index, metric in ((1, "cpu"), (2, "ram")):
            points = [(row[0], row[index]) for row in samples]
            _evaluate_due(session, cluster, host, metric, points[-1][1], now_naive)
            candidates: dict[int, ResourceForecast] = {}
            for window in _candidate_windows():
                windowed = _window_points(points, window)
                prediction = _linear_forecast(
                    windowed, metric,
                    horizon_hours=settings.node_resource_learning_evaluation_hours,
                    training_window_hours=window,
                )
                if prediction is not None:
                    candidates[window] = prediction
            if not candidates:
                continue
            _record_candidates(session, cluster, host, metric, candidates, now_naive)
            selected = _selected_window(session, cluster, host, metric, list(candidates))
            operational = _linear_forecast(
                _window_points(points, selected), metric,
                horizon_hours=settings.node_resource_forecast_horizon_hours,
                training_window_hours=selected,
            )
            if operational is not None:
                result[metric] = operational
        try:
            session.commit()
        except IntegrityError:
            # Concurrent/duplicate hourly scans are idempotent. Replaying the
            # next scan will evaluate the already-persisted row normally.
            session.rollback()
    return result


def forecast(cluster: str, host: str, *, now: datetime | None = None) -> dict[str, ResourceForecast]:
    samples = fetch_samples(cluster, host, now=now)
    result: dict[str, ResourceForecast] = {}
    for index, metric in ((1, "cpu"), (2, "ram")):
        value = _linear_forecast([(row[0], row[index]) for row in samples], metric)
        if value is not None:
            result[metric] = value
    return result


def risky_forecasts(values: dict[str, ResourceForecast]) -> list[ResourceForecast]:
    """Return credible threshold crossings inside the configured horizon."""
    return [value for value in values.values()
            if value.hours_to_90 is not None
            and value.hours_to_90 <= settings.node_resource_forecast_horizon_hours
            and value.confidence >= settings.node_resource_forecast_min_confidence
            and math.isfinite(value.hours_to_90)]
