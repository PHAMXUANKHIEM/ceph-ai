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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import db
from shared.adwin_policy import (
    ADWIN_POLICY_VERSION,
    MAX_REPLAY_SAMPLES,
    AdwinPolicy,
    DriftDecision,
    DriftSample,
)
from shared.metric_quality import MetricQuality, assess_metric_quality
from shared.models import (
    NodeResourceForecastAlert,
    NodeResourceForecastRun,
    NodeResourceDriftState,
    NodeResourceModelState,
    NodeResourceQualityState,
)
from shared.telegram_alerts import send_node_forecast_alert

logger = logging.getLogger(__name__)
JOB = "ceph-ai-node-metrics"
_ADWIN_CACHE: dict[tuple[str, str, str], tuple[str, AdwinPolicy]] = {}


def _adwin_window_size() -> int:
    return min(MAX_REPLAY_SAMPLES, max(32, settings.node_resource_adwin_window_size))


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
    # Plain ``forecast()`` is a read-only compatibility helper.  The adaptive
    # path replaces these defaults with the persisted ADWIN decision before a
    # forecast can reach alerting or promotion.
    drift_status: str = "OK"
    drift_score: float = 0.0
    drift_reason: str = ""
    confidence_multiplier: float = 1.0
    promotion_blocked: bool = False


class NodeResourceLokiError(Exception):
    """The current CPU/RAM observation is absent, stale, or unreadable."""


def _resource_quality(
    samples: list[tuple[datetime, float, float]],
    *,
    now: datetime | None = None,
) -> dict[str, MetricQuality]:
    """Return one shared quality result for CPU and RAM history."""

    timestamps = [row[0] for row in samples]
    common = {
        "now": now,
        "max_age_seconds": max(120, settings.node_health_scan_interval_seconds * 2),
        "minimum_samples": settings.node_resource_forecast_min_samples,
        "expected_interval_seconds": max(1, settings.node_health_scan_interval_seconds),
        "minimum_coverage_ratio": settings.node_resource_quality_min_coverage_ratio,
        "maximum_gap_seconds": settings.node_resource_quality_max_gap_seconds,
        # _linear_forecast() also requires six hours of span. Keeping the
        # quality contract aligned with that lower bound avoids classifying a
        # short but dense burst as valid forecast history.
        "minimum_history_seconds": 6 * 60 * 60,
    }
    return {
        metric: assess_metric_quality(timestamps, **common)
        for metric in ("cpu", "ram")
    }


def _persist_quality_state(
    session,
    cluster: str,
    host: str,
    metric: str,
    quality: MetricQuality,
    *,
    checked_at: datetime,
) -> None:
    """Upsert the quality decision before any forecast candidate is made."""

    row = session.query(NodeResourceQualityState).filter_by(
        cluster_name=cluster, host=host, metric=metric,
    ).one_or_none()
    latest = quality.latest_observed_at
    latest_naive = latest.astimezone(timezone.utc).replace(tzinfo=None) if latest else None
    values = {
        "status": quality.status,
        "latest_observed_at": latest_naive,
        "age_seconds": quality.age_seconds,
        "sample_count": quality.sample_count,
        "history_seconds": quality.history_seconds,
        "coverage_ratio": quality.coverage_ratio,
        "longest_gap_seconds": quality.longest_gap_seconds,
        "reason": quality.reason,
        "checked_at": checked_at,
    }
    if row is None:
        session.add(NodeResourceQualityState(
            cluster_name=cluster, host=host, metric=metric, **values,
        ))
        return
    for name, value in values.items():
        setattr(row, name, value)


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


def _recent_evaluated_runs(session, cluster: str, host: str, metric: str):
    """Return the newest bounded ADWIN window in chronological order.

    The order of the query is intentional: ``DESC + LIMIT 512`` selects the
    latest evidence, and reversing in Python makes River consume it in time
    order.  Querying ascending and limiting first would silently keep the
    oldest 512 rows forever on a long-lived stream.
    """

    rows = (
        session.query(NodeResourceForecastRun)
        .filter_by(cluster_name=cluster, host=host, metric=metric, status="EVALUATED")
        .filter(NodeResourceForecastRun.evaluated_at.isnot(None))
        .filter(NodeResourceForecastRun.actual_percent.isnot(None))
        .filter(NodeResourceForecastRun.absolute_error.isnot(None))
        .order_by(NodeResourceForecastRun.evaluated_at.desc(), NodeResourceForecastRun.id.desc())
        .limit(_adwin_window_size())
        .all()
    )
    return list(reversed(rows))


def _drift_sample(run: NodeResourceForecastRun) -> DriftSample:
    return DriftSample(
        evaluated_at=run.evaluated_at,
        metric_value=run.actual_percent,
        residual=run.actual_percent - run.predicted_percent,
        mae=run.absolute_error,
    )


def _update_drift_state(
    session,
    cluster: str,
    host: str,
    metric: str,
) -> DriftDecision:
    """Apply only new evaluated runs to the scoped, durable ADWIN policy."""

    rows = _recent_evaluated_runs(session, cluster, host, metric)
    row = session.query(NodeResourceDriftState).filter_by(
        cluster_name=cluster,
        host=host,
        metric=metric,
        detector_version=ADWIN_POLICY_VERSION,
    ).one_or_none()
    cache_key = (cluster, host, metric)
    if row is not None:
        cached = _ADWIN_CACHE.get(cache_key)
        try:
            if cached is not None and cached[0] == row.state_json:
                policy = cached[1]
            else:
                policy = AdwinPolicy.from_json(row.state_json)
                _ADWIN_CACHE[cache_key] = (row.state_json, policy)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            # Corrupt state never gets silently repaired inside a promotion
            # decision.  Reset the durable state, but fail closed for this
            # cycle; the next cycle must rebuild evidence from the bounded
            # window instead of allowing a fresh model to promote immediately.
            policy = AdwinPolicy(
                delta=settings.node_resource_adwin_delta,
                min_samples=settings.node_resource_adwin_min_samples,
                clear_consecutive=settings.node_resource_drift_clear_consecutive,
                warmup_samples=settings.node_resource_drift_warmup_samples,
                max_samples=_adwin_window_size(),
            )
            row.state_json = policy.to_json()
            row.last_evaluated_at = None
            row.status = "INSUFFICIENT_DATA"
            row.drift_score = 0.0
            row.confidence_multiplier = 1.0
            row.promotion_blocked = True
            row.reason = "ADWIN state reset after checksum/schema failure; evidence required"
            _ADWIN_CACHE.pop(cache_key, None)
            return DriftDecision(
                status="INSUFFICIENT_DATA",
                promotion_blocked=True,
                reason="ADWIN state reset after checksum/schema failure; evidence required",
            )
        cursor = row.last_evaluated_at
        new_rows = [item for item in rows if cursor is None or item.evaluated_at > cursor]
        if not new_rows:
            return policy.decision()
    else:
        policy = AdwinPolicy(
            delta=settings.node_resource_adwin_delta,
            min_samples=settings.node_resource_adwin_min_samples,
            clear_consecutive=settings.node_resource_drift_clear_consecutive,
            warmup_samples=settings.node_resource_drift_warmup_samples,
            max_samples=_adwin_window_size(),
        )
        _ADWIN_CACHE.pop(cache_key, None)
        new_rows = rows

    decision = policy.decision()
    for item in new_rows:
        decision = policy.update(_drift_sample(item))

    if row is None:
        state_json = policy.to_json()
        row = NodeResourceDriftState(
            cluster_name=cluster,
            host=host,
            metric=metric,
            detector_version=ADWIN_POLICY_VERSION,
            schema_version=1,
            state_json=state_json,
            last_evaluated_at=new_rows[-1].evaluated_at if new_rows else None,
            status=decision.status,
            drift_score=decision.drift_score,
            confidence_multiplier=decision.confidence_multiplier,
            promotion_blocked=decision.promotion_blocked,
            reason=decision.reason,
        )
        session.add(row)
    else:
        state_json = policy.to_json()
        row.state_json = state_json
        row.last_evaluated_at = new_rows[-1].evaluated_at if new_rows else row.last_evaluated_at
        row.status = decision.status
        row.drift_score = decision.drift_score
        row.confidence_multiplier = decision.confidence_multiplier
        row.promotion_blocked = decision.promotion_blocked
        row.reason = decision.reason
    _ADWIN_CACHE[cache_key] = (state_json, policy)
    return decision


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
                       candidates: dict[int, ResourceForecast], now_naive: datetime,
                       drift: DriftDecision) -> None:
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
            confidence=prediction.confidence * drift.confidence_multiplier,
            drift_status=drift.status,
            drift_score=drift.drift_score,
            drift_reason=drift.reason,
            confidence_multiplier=drift.confidence_multiplier,
            promotion_blocked=drift.promotion_blocked,
            status="PENDING", idempotency_key=key,
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
    # The newest actual Loki sample is the observation time.  Using the
    # caller's wall clock here would evaluate forecasts against a stale last
    # sample when Alloy/Loki has stopped shipping data.
    observed_at = samples[-1][0]
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    now_naive = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
    quality = _resource_quality(samples, now=now)
    result: dict[str, ResourceForecast] = {}
    with db.SessionLocal() as session:
        for index, metric in ((1, "cpu"), (2, "ram")):
            metric_quality = quality[metric]
            _persist_quality_state(
                session,
                cluster,
                host,
                metric,
                metric_quality,
                checked_at=datetime.utcnow(),
            )
            if not metric_quality.usable:
                logger.warning(
                    "node forecast: skipping %s for %s because quality=%s: %s",
                    metric,
                    host,
                    metric_quality.status,
                    metric_quality.reason,
                )
                continue
            points = [(row[0], row[index]) for row in samples]
            _evaluate_due(session, cluster, host, metric, points[-1][1], now_naive)
            if settings.node_resource_adwin_enabled:
                drift = _update_drift_state(session, cluster, host, metric)
            else:
                drift = DriftDecision(
                    status="DISABLED",
                    promotion_blocked=True,
                    reason="ADWIN drift policy is disabled by configuration",
                )
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
            _record_candidates(session, cluster, host, metric, candidates, now_naive, drift)
            selected = _selected_window(session, cluster, host, metric, list(candidates))
            operational = _linear_forecast(
                _window_points(points, selected), metric,
                horizon_hours=settings.node_resource_forecast_horizon_hours,
                training_window_hours=selected,
            )
            if operational is not None:
                result[metric] = replace(
                    operational,
                    confidence=max(0.0, min(1.0,
                        operational.confidence * drift.confidence_multiplier)),
                    drift_status=drift.status,
                    drift_score=drift.drift_score,
                    drift_reason=drift.reason,
                    confidence_multiplier=drift.confidence_multiplier,
                    promotion_blocked=drift.promotion_blocked,
                )
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
            if value.drift_status not in {"DRIFT", "METRIC_DRIFT", "WARMUP", "INSUFFICIENT_DATA", "DISABLED"}
            if value.hours_to_90 is not None
            and value.hours_to_90 <= settings.node_resource_forecast_horizon_hours
            and value.confidence >= settings.node_resource_forecast_min_confidence
            and math.isfinite(value.hours_to_90)]


def sync_forecast_alerts(
    cluster: str,
    host: str,
    values: dict[str, ResourceForecast],
    *,
    now: datetime | None = None,
    available_metrics: set[str] | None = None,
) -> None:
    """Open/resolve durable early-warning alerts and enforce Telegram cooldown."""
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    now_naive = reference.astimezone(timezone.utc).replace(tzinfo=None)
    risky = {value.metric: value for value in risky_forecasts(values)}

    with db.SessionLocal() as session:
        existing = {
            row.metric: row
            for row in session.query(NodeResourceForecastAlert).filter_by(
                cluster_name=cluster, host=host
            ).all()
        }
        for metric in ("cpu", "ram"):
            # Missing candidates mean that this metric has insufficient or
            # unavailable Loki history, not that an existing warning is
            # healthy again.  The default None preserves the public helper's
            # existing behaviour for callers that explicitly pass a complete
            # forecast snapshot.
            if available_metrics is not None and metric not in available_metrics:
                continue
            prediction = risky.get(metric)
            alert = existing.get(metric)
            if prediction is None:
                if alert is not None and alert.status == "OPEN":
                    alert.status = "RESOLVED"
                    alert.resolved_at = now_naive
                continue

            if alert is None:
                alert = NodeResourceForecastAlert(
                    cluster_name=cluster,
                    host=host,
                    metric=metric,
                    status="OPEN",
                    first_detected_at=now_naive,
                    last_detected_at=now_naive,
                    current_percent=prediction.current_percent,
                    predicted_percent=prediction.predicted_percent,
                    hours_to_90=prediction.hours_to_90,
                    confidence=prediction.confidence,
                    samples=prediction.samples,
                    window_hours=int(prediction.training_window_hours or prediction.window_hours),
                )
                session.add(alert)
            elif alert.status != "OPEN":
                alert.status = "OPEN"
                alert.first_detected_at = now_naive
                alert.resolved_at = None

            alert.last_detected_at = now_naive
            alert.current_percent = prediction.current_percent
            alert.predicted_percent = prediction.predicted_percent
            alert.hours_to_90 = prediction.hours_to_90
            alert.confidence = prediction.confidence
            alert.samples = prediction.samples
            alert.window_hours = int(prediction.training_window_hours or prediction.window_hours)

            cooldown = max(0, settings.node_resource_forecast_alert_cooldown_seconds)
            due = (
                alert.last_notified_at is None
                or (now_naive - alert.last_notified_at).total_seconds() >= cooldown
            )
            if due and send_node_forecast_alert(
                host,
                metric,
                prediction.current_percent,
                prediction.predicted_percent,
                prediction.hours_to_90,
                prediction.confidence,
                prediction.samples,
                int(prediction.training_window_hours or prediction.window_hours),
                cluster_name=cluster,
            ):
                alert.last_notified_at = now_naive
        session.commit()
