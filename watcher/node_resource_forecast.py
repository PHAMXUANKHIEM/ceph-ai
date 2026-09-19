"""Loki-backed CPU/RAM monitoring and deterministic resource forecasting.

Loki is the source of truth. Alloy publishes the node-resource stream and
both current threshold monitoring and forecasting read that same stream;
the Watcher does not SSH to nodes to manufacture CPU/RAM observations.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import statistics
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect, select

from config.settings import settings
from shared import db
from shared.predictive_alert_lifecycle import (
    AlertLifecycleState,
    NotificationState,
    next_lifecycle_state,
)
from shared.forecast_consensus import ForecastConsensus, aggregate_forecasts
from shared.forecast_drift import DriftReport, evaluate_drift
from shared.forecast_metrics import update_rolling_metrics
from shared.learning_runtime import evaluate as evaluate_learning_runtime
from shared.models import (
    Cluster,
    NodeResourceForecastAlert,
    NodeResourceForecastAlertEvent,
    NodeResourceForecastRun,
    NodeResourceForecastTransition,
    NodeResourceModelState,
)
from shared.online_learning import MODEL_VERSION, load_or_reset_state
from shared.telegram_alerts import send_node_forecast_alert

logger = logging.getLogger(__name__)
JOB = "ceph-ai-node-metrics"


def _forecast_alert_events_table_available(session) -> bool:
    """Return whether the post-RR-03 event table is installed."""
    try:
        return bool(inspect(session.get_bind()).has_table("node_resource_forecast_alert_events"))
    except Exception:
        return False


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
    # Quality gates prevent a good-looking line fit over sparse or interrupted
    # history from becoming an operational warning.
    coverage_ratio: float = 1.0
    max_gap_hours: float = 0.0
    consensus_ratio: float = 1.0
    consensus_candidate_count: int = 1
    consensus_status: str = "LEGACY"
    predicted_low: float | None = None
    predicted_high: float | None = None
    residual_percent: float = 0.0
    anomaly_score: float = 0.0
    drift_status: str = "INSUFFICIENT_DATA"
    drift_score: float = 0.0
    drift_reason: str = ""


class NodeResourceLokiError(Exception):
    """The current CPU/RAM observation is absent, stale, or unreadable."""


def _resource_consensus(values: list[ResourceForecast]) -> ForecastConsensus:
    return aggregate_forecasts(
        [value.predicted_percent for value in values],
        minimum_candidates=settings.node_resource_forecast_min_consensus_candidates,
        minimum_ratio=settings.node_resource_forecast_min_consensus_ratio,
        absolute_tolerance=settings.node_resource_forecast_consensus_tolerance_percent,
        relative_tolerance=settings.node_resource_forecast_consensus_relative_tolerance,
    )


def _drift_report(session, cluster: str, host: str, metric: str, now: datetime) -> DriftReport:
    """Compare recent forecast evidence with the preceding bounded window."""

    rows = list(session.query(NodeResourceForecastRun).filter(
        NodeResourceForecastRun.cluster_name == cluster,
        NodeResourceForecastRun.host == host,
        NodeResourceForecastRun.metric == metric,
        NodeResourceForecastRun.predicted_at < now,
    ).order_by(NodeResourceForecastRun.predicted_at).all())
    rows = rows[-max(2, int(settings.forecast_drift_history_runs)):]
    midpoint = len(rows) // 2
    baseline, recent = rows[:midpoint], rows[midpoint:]
    threshold = settings.node_resource_forecast_trigger_threshold_percent
    return evaluate_drift(
        [row.current_percent for row in baseline],
        [row.current_percent for row in recent],
        baseline_residuals=[row.residual_percent for row in baseline],
        recent_residuals=[row.residual_percent for row in recent],
        baseline_coverages=[row.coverage_ratio for row in baseline],
        recent_coverages=[row.coverage_ratio for row in recent],
        baseline_alerts=[row.predicted_percent >= threshold for row in baseline],
        recent_alerts=[row.predicted_percent >= threshold for row in recent],
        minimum_samples=settings.forecast_drift_minimum_samples,
        baseline_shift_threshold=settings.forecast_drift_baseline_shift_threshold,
        residual_shift_threshold=settings.forecast_drift_residual_shift_threshold,
        coverage_drop_threshold=settings.forecast_drift_coverage_drop_threshold,
        alert_rate_increase_threshold=settings.forecast_drift_alert_rate_increase_threshold,
        drift_confidence_multiplier=settings.forecast_drift_confidence_multiplier,
    )


def _river_shadow_candidate(
    session, cluster: str, host: str, metric: str, current_percent: float,
) -> ResourceForecast | None:
    """Return a River forecast only as an auditable shadow candidate.

    This function never feeds ``operational_candidates``. The deterministic
    ensemble therefore remains the active alert source until an explicit,
    guarded promotion is approved.
    """

    if not settings.online_learning_enabled:
        return None
    cluster_row = session.scalar(select(Cluster).where(Cluster.name == cluster))
    cluster_id = cluster_row.id if cluster_row is not None else None
    runtime = evaluate_learning_runtime(session, cluster_id)
    if not runtime.can_observe:
        return None
    learner, state = load_or_reset_state(
        session,
        cluster_id=cluster_id,
        host=host,
        metric=metric,
        model_version=MODEL_VERSION,
    )
    if learner.sample_count < max(1, int(settings.online_learning_min_verified_evidence)):
        return None
    predicted = learner.predict_one(fallback=current_percent)
    if predicted is None or not math.isfinite(float(predicted)):
        return None
    return ResourceForecast(
        metric=metric,
        current_percent=float(current_percent),
        slope_percent_per_hour=0.0,
        predicted_percent=max(0.0, min(100.0, float(predicted))),
        hours_to_90=0.0 if float(predicted) >= 90.0 else None,
        confidence=0.5,
        samples=learner.sample_count,
        window_hours=1.0,
        algorithm="river_mean",
        training_window_hours=1,
        coverage_ratio=1.0,
        max_gap_hours=0.0,
        consensus_status="SHADOW_CANDIDATE",
    )


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    left = math.floor(position)
    right = min(len(ordered) - 1, left + 1)
    fraction = position - left
    return ordered[left] + (ordered[right] - ordered[left]) * fraction


def _robust_baseline(values: list[float]) -> tuple[float, float, float]:
    """Return median, MAD and a bounded standardized residual scale."""

    median = float(statistics.median(values)) if values else 0.0
    mad = float(statistics.median([abs(value - median) for value in values])) if values else 0.0
    return median, mad, max(1.0, 1.4826 * mad)


def _residual_features(values: list[float]) -> tuple[float, float]:
    if len(values) < 2:
        return 0.0, 0.0
    baseline_values = values[:-1] or values
    baseline, _mad, scale = _robust_baseline(baseline_values)
    residual = values[-1] - baseline
    return residual, min(20.0, abs(residual) / scale)


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
    try:
        response = httpx.get(f"{_base_url()}/loki/api/v1/query_range", params=params,
                             headers=_headers(), timeout=settings.log_intel_loki_timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, TypeError, ValueError) as exc:
        # Callers use NodeResourceLokiError to isolate one unavailable Loki
        # query from the remaining nodes in a health scan.  Do not leak a
        # transport, status, or malformed-JSON exception across that boundary.
        raise NodeResourceLokiError(f"{host}: không truy vấn được CPU/RAM từ Loki: {exc}") from exc
    if not isinstance(payload, dict):
        raise NodeResourceLokiError(f"{host}: Loki trả về payload CPU/RAM không hợp lệ")
    rows: dict[int, tuple[datetime, float, float]] = {}
    for stream in ((payload.get("data") or {}).get("result") or []):
        if not isinstance(stream, dict):
            continue
        for ts_ns, line in stream.get("values") or []:
            try:
                raw = json.loads(line)
                ts_int = int(ts_ns)
                cpu = float(raw["cpu_percent"])
                mem = float(raw["mem_percent"])
                if not (math.isfinite(cpu) and math.isfinite(mem)):
                    continue
                if not (0.0 <= cpu <= 100.0 and 0.0 <= mem <= 100.0):
                    continue
                rows[ts_int] = (datetime.fromtimestamp(ts_int / 1e9, timezone.utc), cpu, mem)
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
    if age_seconds < -300:
        raise NodeResourceLokiError(
            f"{host}: latest Loki CPU/RAM sample is from the future ({int(-age_seconds)}s ahead)"
        )
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
    gaps = [right - left for left, right in zip(xs, xs[1:])]
    max_gap_hours = max(gaps) if gaps else 0.0
    expected_window = max(1.0, float(training_window_hours or window))
    coverage_ratio = max(0.0, min(1.0, window / expected_window))
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
    residual_percent, anomaly_score = _residual_features(ys)
    residuals = [y - fit for y, fit in zip(ys, fitted)]
    predicted_low = max(0.0, min(100.0, predicted + _quantile(residuals, 0.10)))
    predicted_high = max(0.0, min(100.0, predicted + _quantile(residuals, 0.90)))
    if predicted_low > predicted_high:
        predicted_low, predicted_high = predicted_high, predicted_low
    hours_to_90 = None
    trigger_threshold = settings.node_resource_forecast_trigger_threshold_percent
    if slope > 0 and ys[-1] < trigger_threshold:
        crossing = (trigger_threshold - intercept) / slope
        if crossing >= xs[-1]:
            hours_to_90 = crossing - xs[-1]
    return ResourceForecast(
        metric, ys[-1], slope, predicted, hours_to_90,
        confidence, len(points), window,
        training_window_hours=training_window_hours,
        coverage_ratio=coverage_ratio,
        max_gap_hours=max_gap_hours,
        predicted_low=predicted_low,
        predicted_high=predicted_high,
        residual_percent=residual_percent,
        anomaly_score=anomaly_score,
    )


def _rolling_quantile_forecast(
    points: list[tuple[datetime, float]], metric: str, *, horizon_hours: int | None = None,
    training_window_hours: int | None = None,
) -> ResourceForecast | None:
    """Robust candidate using recent median and empirical quantiles.

    It deliberately has no trend extrapolation.  That makes it independent
    from the linear candidate and useful as a conservative vote when a line
    is being pulled by a short spike.
    """

    minimum = max(3, settings.node_resource_forecast_min_samples)
    if len(points) < minimum:
        return None
    timestamps = [timestamp for timestamp, _value in points]
    values = [float(value) for _timestamp, value in points]
    window = (timestamps[-1] - timestamps[0]).total_seconds() / 3600
    if window < 6:
        return None
    robust_values = values[-min(len(values), 24):]
    predicted, mad, scale = _robust_baseline(robust_values)
    residual_percent, anomaly_score = _residual_features(values)
    confidence = min(1.0, len(robust_values) / max(minimum, 24))
    confidence *= max(0.0, min(1.0, 1.0 - mad / max(abs(predicted), 1.0)))
    horizon = max(1, horizon_hours or settings.node_resource_forecast_horizon_hours)
    del horizon  # the robust baseline is intentionally horizon-flat
    low = max(0.0, min(100.0, _quantile(robust_values, 0.10)))
    high = max(0.0, min(100.0, _quantile(robust_values, 0.90)))
    return ResourceForecast(
        metric=metric,
        current_percent=values[-1],
        slope_percent_per_hour=0.0,
        predicted_percent=max(0.0, min(100.0, predicted)),
        hours_to_90=0.0 if predicted >= settings.node_resource_forecast_trigger_threshold_percent else None,
        confidence=round(confidence, 6),
        samples=len(points),
        window_hours=window,
        algorithm="rolling_quantile",
        training_window_hours=training_window_hours,
        coverage_ratio=1.0,
        max_gap_hours=max(
            ((right - left).total_seconds() / 3600)
            for left, right in zip(timestamps, timestamps[1:])
        ) if len(timestamps) > 1 else 0.0,
        predicted_low=min(low, high),
        predicted_high=max(low, high),
        residual_percent=residual_percent,
        anomaly_score=anomaly_score,
    )


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


def _state_for(
    session, cluster: str, host: str, metric: str, window_hours: int,
    algorithm: str = "linear",
):
    state = session.query(NodeResourceModelState).filter_by(
        cluster_name=cluster, host=host, metric=metric,
        algorithm=algorithm, window_hours=window_hours,
    ).one_or_none()
    if state is None:
        state = NodeResourceModelState(
            cluster_name=cluster, host=host, metric=metric,
            algorithm=algorithm, window_hours=window_hours,
        )
        session.add(state)
        session.flush()
    return state


def _evaluate_due(
    session, cluster: str, host: str, metric: str,
    actual_percent: float | None, now_naive: datetime,
    points: list[tuple[datetime, float]] | None = None,
) -> None:
    due = session.query(NodeResourceForecastRun).filter_by(
        cluster_name=cluster, host=host, metric=metric, status="PENDING"
    ).filter(NodeResourceForecastRun.target_at <= now_naive).all()
    max_gap = max(0.25, float(settings.node_resource_learning_max_outcome_gap_hours))
    for run in due:
        actual = actual_percent
        if points:
            target = run.target_at.replace(tzinfo=timezone.utc)
            nearest = min(points, key=lambda point: abs(point[0] - target))
            if abs((nearest[0] - target).total_seconds()) <= max_gap * 3600:
                actual = nearest[1]
            else:
                actual = None
        elif actual is not None:
            age_hours = (now_naive - run.target_at).total_seconds() / 3600
            if age_hours > max_gap:
                actual = None
        if actual is None:
            # Do not score a forecast against a much-later observation. The
            # missing target-time sample is a data-quality outcome, not a
            # model failure and must not affect MAE/window selection.
            if (now_naive - run.target_at).total_seconds() > max_gap * 3600:
                run.status = "UNMEASURABLE"
                run.evaluated_at = now_naive
            continue
        error = abs(run.predicted_percent - actual)
        run.actual_percent = actual
        run.absolute_error = error
        run.status = "EVALUATED"
        run.evaluated_at = now_naive
        state = _state_for(
            session, cluster, host, metric, run.window_hours, run.algorithm,
        )
        old_count = state.evaluated_count
        old_mae = state.mean_absolute_error or 0.0
        state.evaluated_count = old_count + 1
        state.mean_absolute_error = (old_mae * old_count + error) / state.evaluated_count
        state.last_absolute_error = error
        state.rolling_metrics_json, rolling = update_rolling_metrics(
            state.rolling_metrics_json,
            run.predicted_percent,
            actual,
            limit=settings.node_resource_learning_rolling_samples,
        )
        state.rolling_sample_count = int(rolling["count"])
        state.rolling_mae = float(rolling["mae"])
        state.rolling_rmse = float(rolling["rmse"])
        state.rolling_smape = float(rolling["smape"])
        state.rolling_bias = float(rolling["bias"])


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


def _record_candidates(
    session, cluster: str, host: str, metric: str,
    candidates: list[ResourceForecast], now_naive: datetime,
    consensus: ForecastConsensus, drift: DriftReport | None = None,
) -> None:
    horizon = max(1, settings.node_resource_learning_evaluation_hours)
    bucket = now_naive.replace(minute=0, second=0, microsecond=0)
    votes = json.dumps([
        {
            "algorithm": prediction.algorithm,
            "window_hours": prediction.training_window_hours,
            "predicted_percent": round(prediction.predicted_percent, 6),
            "confidence": round(prediction.confidence, 6),
        }
        for prediction in candidates
    ], separators=(",", ":"), sort_keys=True)
    for prediction in candidates:
        window = int(prediction.training_window_hours or prediction.window_hours)
        key = f"{cluster}|{host}|{metric}|{prediction.algorithm}|{window}|{bucket.isoformat()}"
        exists = session.query(NodeResourceForecastRun.id).filter_by(idempotency_key=key).first()
        if exists:
            continue
        session.add(NodeResourceForecastRun(
            cluster_name=cluster, host=host, metric=metric, algorithm=prediction.algorithm,
            window_hours=window, predicted_at=now_naive,
            target_at=now_naive + timedelta(hours=horizon),
            current_percent=prediction.current_percent,
            predicted_percent=prediction.predicted_percent,
            confidence=prediction.confidence, status="PENDING", idempotency_key=key,
            consensus_status=consensus.status,
            consensus_ratio=consensus.ratio,
            consensus_candidate_count=consensus.candidate_count,
            predicted_low=consensus.lower,
            predicted_high=consensus.upper,
            residual_percent=prediction.residual_percent,
            anomaly_score=prediction.anomaly_score,
            coverage_ratio=prediction.coverage_ratio,
            max_gap_hours=prediction.max_gap_hours,
            drift_status=drift.status if drift is not None else "INSUFFICIENT_DATA",
            drift_score=drift.score if drift is not None else 0.0,
            drift_reason=drift.reason if drift is not None else None,
            model_votes_json=votes,
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
            _evaluate_due(session, cluster, host, metric, points[-1][1], now_naive, points)
            linear_candidates: dict[int, ResourceForecast] = {}
            for window in _candidate_windows():
                windowed = _window_points(points, window)
                prediction = _linear_forecast(
                    windowed, metric,
                    horizon_hours=settings.node_resource_learning_evaluation_hours,
                    training_window_hours=window,
                )
                if prediction is not None:
                    linear_candidates[window] = prediction
            if not linear_candidates:
                continue
            selected = _selected_window(session, cluster, host, metric, list(linear_candidates))
            operational_candidates: list[ResourceForecast] = []
            operational_linear: ResourceForecast | None = None
            for window, linear in linear_candidates.items():
                if (
                    linear.coverage_ratio < settings.node_resource_forecast_min_coverage
                    or linear.max_gap_hours > settings.node_resource_forecast_max_gap_hours
                ):
                    continue
                rolling = _rolling_quantile_forecast(
                    _window_points(points, window), metric,
                    horizon_hours=settings.node_resource_forecast_horizon_hours,
                    training_window_hours=window,
                )
                operational_candidates.append(linear)
                if rolling is not None:
                    operational_candidates.append(rolling)
                if window == selected:
                    operational_linear = linear
            if operational_linear is None:
                # A selected model whose quality gate failed cannot be used as
                # an operational fallback.  The remaining models may still
                # form a consensus, but they must pass the same gate.
                linear_candidates = {
                    int(candidate.training_window_hours or candidate.window_hours): candidate
                    for candidate in operational_candidates
                    if candidate.algorithm == "linear"
                }
                if not linear_candidates:
                    continue
                selected = max(linear_candidates)
                operational_linear = linear_candidates[selected]
            if operational_candidates:
                consensus = _resource_consensus(operational_candidates)
                drift = _drift_report(session, cluster, host, metric, now_naive)
                river_candidate = _river_shadow_candidate(
                    session, cluster, host, metric, operational_linear.current_percent,
                )
                recorded_candidates = operational_candidates + (
                    [river_candidate] if river_candidate is not None else []
                )
                _record_candidates(
                    session, cluster, host, metric, recorded_candidates,
                    now_naive, consensus, drift,
                )
                result[metric] = replace(
                    operational_linear,
                    confidence=max(
                        0.0,
                        min(1.0, operational_linear.confidence * drift.confidence_multiplier),
                    ),
                    predicted_percent=(
                        consensus.value if consensus.candidate_count else operational_linear.predicted_percent
                    ),
                    consensus_ratio=consensus.ratio,
                    consensus_candidate_count=consensus.candidate_count,
                    consensus_status=consensus.status,
                    predicted_low=consensus.lower if consensus.candidate_count else None,
                    predicted_high=consensus.upper if consensus.candidate_count else None,
                    drift_status=drift.status,
                    drift_score=drift.score,
                    drift_reason=drift.reason,
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
    def crosses_threshold(value: ResourceForecast) -> bool:
        threshold = settings.node_resource_forecast_trigger_threshold_percent
        return (
            (
                value.hours_to_90 is not None
                and value.hours_to_90 <= settings.node_resource_forecast_horizon_hours
            )
            or (
                value.predicted_high is not None
                and value.predicted_high >= threshold
            )
        )

    return [value for value in values.values()
            if crosses_threshold(value)
            and value.confidence >= settings.node_resource_forecast_min_confidence
            and value.coverage_ratio >= settings.node_resource_forecast_min_coverage
            and value.max_gap_hours <= settings.node_resource_forecast_max_gap_hours
            and value.consensus_status not in {"LOW_CONFIDENCE", "INSUFFICIENT_CANDIDATES"}
            and value.drift_status != "DRIFT"
            and (
                value.consensus_status == "LEGACY"
                or value.consensus_ratio >= settings.node_resource_forecast_min_consensus_ratio
            )
            and (
                value.hours_to_90 is None
                or math.isfinite(value.hours_to_90)
            )]


def _above_recovery_threshold(value: ResourceForecast) -> bool:
    upper_bound = value.predicted_high
    if upper_bound is None:
        upper_bound = value.predicted_percent
    return upper_bound >= settings.node_resource_forecast_recovery_threshold_percent


def anomaly_candidates(values: dict[str, ResourceForecast]) -> list[ResourceForecast]:
    """Return signal-bearing forecasts that are not safe to promote to WARNING.

    A candidate is persisted for operator visibility, but it is deliberately
    excluded from Telegram notification and remediation paths until consensus
    and confidence gates pass.
    """

    return [value for value in values.values()
            if value.drift_status == "DRIFT"
            or value.consensus_status in {"LOW_CONFIDENCE", "INSUFFICIENT_CANDIDATES"}
            and (
                value.hours_to_90 is not None
                or (
                    value.predicted_high is not None
                    and value.predicted_high >= settings.node_resource_forecast_trigger_threshold_percent
                )
                or value.anomaly_score >= 3.0
            )]


def _legacy_lifecycle_state(alert: NodeResourceForecastAlert | None) -> str | None:
    if alert is None:
        return None
    state = getattr(alert, "lifecycle_state", None)
    if state and not (state == AlertLifecycleState.NORMAL.value and alert.status == "OPEN"):
        return state
    if alert.status == "OPEN":
        return AlertLifecycleState.WARNING.value
    if alert.status == "DATA_QUALITY":
        return AlertLifecycleState.DATA_QUALITY.value
    if alert.status == "CANDIDATE":
        return AlertLifecycleState.CANDIDATE.value
    return state or AlertLifecycleState.NORMAL.value


def _set_lifecycle(
    alert: NodeResourceForecastAlert,
    state: str,
    now: datetime,
    reason: str,
    *,
    evidence_version: str | None = None,
) -> None:
    previous = _legacy_lifecycle_state(alert)
    if previous != state or alert.state_changed_at is None:
        alert.state_changed_at = now
    alert.lifecycle_state = state
    alert.state_reason = reason
    if evidence_version is not None:
        alert.evidence_version = evidence_version
    # Keep the legacy status column readable for existing dashboard/API code;
    # lifecycle_state is the authoritative state after this migration.
    if state == AlertLifecycleState.DATA_QUALITY.value:
        alert.status = "DATA_QUALITY"
    elif state in {
        AlertLifecycleState.WARNING.value,
        AlertLifecycleState.CRITICAL.value,
    }:
        alert.status = "OPEN"
    elif state == AlertLifecycleState.CANDIDATE.value:
        alert.status = "CANDIDATE"
    elif state == AlertLifecycleState.RECOVERING.value:
        alert.status = "RECOVERING"
    else:
        alert.status = "RESOLVED"


def _transition_lifecycle(
    alert: NodeResourceForecastAlert,
    now: datetime,
    *,
    quality_ok: bool,
    candidate: bool = False,
    warning: bool = False,
    critical: bool = False,
    reason: str,
    evidence_version: str | None = None,
) -> str:
    state = next_lifecycle_state(
        _legacy_lifecycle_state(alert), quality_ok=quality_ok,
        candidate=candidate, warning=warning, critical=critical,
    )
    _set_lifecycle(alert, state, now, reason, evidence_version=evidence_version)
    return state


def _evidence_fingerprint(
    cluster: str, host: str, prediction: ResourceForecast,
) -> str:
    payload = "|".join((
        cluster,
        host,
        prediction.metric,
        prediction.consensus_status,
        f"{prediction.consensus_ratio:.6f}",
        f"{prediction.predicted_percent:.6f}",
        f"{prediction.predicted_low or 0.0:.6f}",
        f"{prediction.predicted_high or 0.0:.6f}",
        f"{prediction.training_window_hours or prediction.window_hours}",
        prediction.drift_status,
        f"{prediction.drift_score:.6f}",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quality_evidence_summary(prediction: ResourceForecast) -> str:
    """Keep model vote, interval and quality visible in lifecycle history."""

    upper = prediction.predicted_high if prediction.predicted_high is not None else prediction.predicted_percent
    lower = prediction.predicted_low if prediction.predicted_low is not None else prediction.predicted_percent
    return (
        f"models={prediction.consensus_candidate_count}; "
        f"consensus={prediction.consensus_status}/{prediction.consensus_ratio:.3f}; "
        f"confidence={prediction.confidence:.3f}; interval=[{lower:.1f},{upper:.1f}]; "
        f"quality=coverage:{prediction.coverage_ratio:.3f},gap:{prediction.max_gap_hours:.2f}h,"
        f"drift:{prediction.drift_status}/{prediction.drift_score:.1f}"
    )


def _alert_cooldown_seconds(*, critical: bool) -> int:
    """Return the severity-specific notification cooldown.

    Keep the old setting as a safe fallback for older Settings objects used by
    tests or rolling deployments.
    """
    setting_name = (
        "node_resource_forecast_critical_cooldown_seconds"
        if critical
        else "node_resource_forecast_warning_cooldown_seconds"
    )
    configured = getattr(settings, setting_name, None)
    if configured is None:
        configured = getattr(settings, "node_resource_forecast_alert_cooldown_seconds", 0)
    return max(0, int(configured))


def sync_forecast_alerts(
    cluster: str,
    host: str,
    values: dict[str, ResourceForecast],
    *,
    now: datetime | None = None,
    suppressed_until: datetime | None = None,
) -> None:
    """Open/resolve durable early-warning alerts and enforce Telegram cooldown."""
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    now_naive = reference.astimezone(timezone.utc).replace(tzinfo=None)
    if suppressed_until is not None and suppressed_until.tzinfo is not None:
        suppressed_until = suppressed_until.astimezone(timezone.utc).replace(tzinfo=None)
    risky = {value.metric: value for value in risky_forecasts(values)}
    candidates = {value.metric: value for value in anomaly_candidates(values)}

    def quality_blocked(value: ResourceForecast | None) -> bool:
        return value is None or (
            value.coverage_ratio < settings.node_resource_forecast_min_coverage
            or value.max_gap_hours > settings.node_resource_forecast_max_gap_hours
            or value.drift_status == "DRIFT"
        )

    with db.SessionLocal() as session:
        existing = {
            row.metric: row
            for row in session.query(NodeResourceForecastAlert).filter_by(
                cluster_name=cluster, host=host
            ).all()
        }
        previous_states = {
            metric: _legacy_lifecycle_state(alert)
            for metric, alert in existing.items()
        }
        for metric in ("cpu", "ram"):
            prediction = risky.get(metric)
            alert = existing.get(metric)
            if suppressed_until is not None and suppressed_until > now_naive:
                if alert is not None:
                    alert.suppressed_until = suppressed_until
                    _transition_lifecycle(
                        alert,
                        now_naive,
                        quality_ok=True,
                        reason="Predictive alert đang trong maintenance suppression window.",
                        evidence_version="maintenance:v1",
                    )
                    alert.lifecycle_state = AlertLifecycleState.SUPPRESSED.value
                    alert.status = "RESOLVED"
                    alert.notification_state = NotificationState.SUPPRESSED.value
                continue
            if prediction is None:
                candidate = values.get(metric)
                if quality_blocked(candidate):
                    if (
                        alert is None
                        and candidate is not None
                        and candidate.drift_status == "DRIFT"
                    ):
                        window_hours = int(candidate.training_window_hours or candidate.window_hours)
                        alert = NodeResourceForecastAlert(
                            cluster_name=cluster,
                            host=host,
                            metric=metric,
                            status="DATA_QUALITY",
                            first_detected_at=now_naive,
                            last_detected_at=now_naive,
                            current_percent=candidate.current_percent,
                            predicted_percent=candidate.predicted_percent,
                            hours_to_90=candidate.hours_to_90 or 0.0,
                            confidence=candidate.confidence,
                            samples=candidate.samples,
                            window_hours=window_hours,
                            consensus_status=candidate.consensus_status,
                            consensus_ratio=candidate.consensus_ratio,
                            consensus_candidate_count=candidate.consensus_candidate_count,
                            predicted_low=candidate.predicted_low,
                            predicted_high=candidate.predicted_high,
                            anomaly_score=candidate.anomaly_score,
                        )
                        session.add(alert)
                    reason = "Nguồn metric không đạt data-quality gate."
                    if candidate is not None and candidate.drift_status == "DRIFT":
                        reason = f"Concept drift phát hiện; giữ DATA_QUALITY. {candidate.drift_reason}"
                    if candidate is not None:
                        reason = f"{reason} {_quality_evidence_summary(candidate)}"
                    if alert is not None:
                        _transition_lifecycle(
                            alert,
                            now_naive,
                            quality_ok=False,
                            reason=reason,
                            evidence_version="quality-gate:v1",
                        )
                        alert.notification_state = NotificationState.SUPPRESSED.value
                        alert.resolved_at = None
                    if alert is not None:
                        logger.warning(
                            "node forecast: %s %s alert held as DATA_QUALITY (coverage=%.3f gap=%.2fh)",
                            host, metric.upper(),
                            candidate.coverage_ratio if candidate is not None else 0.0,
                            candidate.max_gap_hours if candidate is not None else float("inf"),
                        )
                    continue
                anomaly = candidates.get(metric)
                if anomaly is not None:
                    window_hours = int(anomaly.training_window_hours or anomaly.window_hours)
                    if alert is None:
                        alert = NodeResourceForecastAlert(
                            cluster_name=cluster,
                            host=host,
                            metric=metric,
                            status="CANDIDATE",
                            first_detected_at=now_naive,
                            last_detected_at=now_naive,
                            current_percent=anomaly.current_percent,
                            predicted_percent=anomaly.predicted_percent,
                            hours_to_90=anomaly.hours_to_90 or 0.0,
                            confidence=anomaly.confidence,
                            samples=anomaly.samples,
                            window_hours=window_hours,
                            consensus_status=anomaly.consensus_status,
                            consensus_ratio=anomaly.consensus_ratio,
                            consensus_candidate_count=anomaly.consensus_candidate_count,
                            predicted_low=anomaly.predicted_low,
                            predicted_high=anomaly.predicted_high,
                            anomaly_score=anomaly.anomaly_score,
                        )
                        session.add(alert)
                    else:
                        alert.status = "CANDIDATE"
                        alert.last_detected_at = now_naive
                        alert.current_percent = anomaly.current_percent
                        alert.predicted_percent = anomaly.predicted_percent
                        alert.hours_to_90 = anomaly.hours_to_90 or 0.0
                        alert.confidence = anomaly.confidence
                        alert.samples = anomaly.samples
                        alert.window_hours = window_hours
                        alert.consensus_status = anomaly.consensus_status
                        alert.consensus_ratio = anomaly.consensus_ratio
                        alert.consensus_candidate_count = anomaly.consensus_candidate_count
                        alert.predicted_low = anomaly.predicted_low
                        alert.predicted_high = anomaly.predicted_high
                        alert.anomaly_score = anomaly.anomaly_score
                    alert.evidence_fingerprint = _evidence_fingerprint(cluster, host, anomaly)
                    _transition_lifecycle(
                        alert,
                        now_naive,
                        quality_ok=True,
                        candidate=True,
                        reason=(
                            f"Consensus chưa đủ: {anomaly.consensus_ratio:.3f} ratio; "
                            f"anomaly score {anomaly.anomaly_score:.2f}; "
                            f"{_quality_evidence_summary(anomaly)}"
                        ),
                        evidence_version="consensus:v1",
                    )
                    alert.notification_state = NotificationState.SUPPRESSED.value
                    alert.resolved_at = None
                    # No notification or remediation is allowed for a
                    # low-consensus candidate.
                    continue
                if alert is not None:
                    previous_state = _legacy_lifecycle_state(alert)
                    if (
                        candidate is not None
                        and previous_state in {
                            AlertLifecycleState.WARNING.value,
                            AlertLifecycleState.CRITICAL.value,
                            AlertLifecycleState.RECOVERING.value,
                        }
                        and _above_recovery_threshold(candidate)
                    ):
                        # The forecast is below the trigger threshold but has
                        # not crossed the lower recovery threshold yet.
                        # Preserve the open lifecycle instead of oscillating
                        # through RECOVERING on every scan near 90%.
                        alert.last_detected_at = now_naive
                        alert.consecutive_healthy_count = 0
                        alert.resolved_at = None
                        _set_lifecycle(
                            alert,
                            previous_state,
                            now_naive,
                            (
                                "Forecast đã dưới ngưỡng trigger nhưng upper bound "
                                "chưa dưới ngưỡng recovery; giữ cảnh báo để tránh flap."
                            ),
                            evidence_version="value-hysteresis:v1",
                        )
                        alert.notification_state = NotificationState.COOLDOWN.value
                        continue
                    healthy_count = int(alert.consecutive_healthy_count or 0) + 1
                    alert.consecutive_healthy_count = healthy_count
                    alert.consecutive_breach_count = 0
                    state = _transition_lifecycle(
                        alert,
                        now_naive,
                        quality_ok=True,
                        reason="Không còn evidence cảnh báo ở lần quét này.",
                        evidence_version="reconcile:v1",
                    )
                    alert.notification_state = NotificationState.IDLE.value
                    if previous_state in {
                        AlertLifecycleState.WARNING.value,
                        AlertLifecycleState.CRITICAL.value,
                        AlertLifecycleState.RECOVERING.value,
                    } and healthy_count < max(
                        1, settings.node_resource_forecast_recovery_consecutive_scans
                    ):
                        _set_lifecycle(
                            alert,
                            AlertLifecycleState.RECOVERING.value,
                            now_naive,
                            "Đang chờ đủ các lần healthy liên tiếp để recovery.",
                            evidence_version="recovery-hysteresis:v1",
                        )
                        alert.resolved_at = None
                    elif previous_state in {
                        AlertLifecycleState.WARNING.value,
                        AlertLifecycleState.CRITICAL.value,
                        AlertLifecycleState.RECOVERING.value,
                    } and healthy_count >= max(
                        1, settings.node_resource_forecast_recovery_consecutive_scans
                    ):
                        _set_lifecycle(
                            alert,
                            AlertLifecycleState.RECOVERED.value,
                            now_naive,
                            "Đã healthy đủ số lần liên tiếp để recovery.",
                            evidence_version="recovery-hysteresis:v1",
                        )
                        alert.resolved_at = now_naive
                    elif state in {
                        AlertLifecycleState.RECOVERED.value,
                        AlertLifecycleState.NORMAL.value,
                    }:
                        alert.resolved_at = now_naive
                continue

            window_hours = int(prediction.training_window_hours or prediction.window_hours)
            effective_hours_to_90 = (
                prediction.hours_to_90
                if prediction.hours_to_90 is not None
                else settings.node_resource_forecast_horizon_hours
            )
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
                    hours_to_90=effective_hours_to_90,
                    confidence=prediction.confidence,
                    samples=prediction.samples,
                    window_hours=window_hours,
                    consensus_status=prediction.consensus_status,
                    consensus_ratio=prediction.consensus_ratio,
                    consensus_candidate_count=prediction.consensus_candidate_count,
                    predicted_low=prediction.predicted_low,
                    predicted_high=prediction.predicted_high,
                    anomaly_score=prediction.anomaly_score,
                )
                session.add(alert)
            elif alert.status != "OPEN":
                alert.status = "OPEN"
                alert.first_detected_at = now_naive
                alert.resolved_at = None

            alert.last_detected_at = now_naive
            alert.current_percent = prediction.current_percent
            alert.predicted_percent = prediction.predicted_percent
            alert.hours_to_90 = effective_hours_to_90
            alert.confidence = prediction.confidence
            alert.samples = prediction.samples
            alert.window_hours = window_hours
            alert.consensus_status = prediction.consensus_status
            alert.consensus_ratio = prediction.consensus_ratio
            alert.consensus_candidate_count = prediction.consensus_candidate_count
            alert.predicted_low = prediction.predicted_low
            alert.predicted_high = prediction.predicted_high
            alert.anomaly_score = prediction.anomaly_score
            alert.evidence_fingerprint = _evidence_fingerprint(cluster, host, prediction)
            critical = (
                prediction.predicted_percent >= 95.0
                or (prediction.predicted_high is not None and prediction.predicted_high >= 95.0)
            )
            alert.consecutive_breach_count = int(alert.consecutive_breach_count or 0) + 1
            alert.consecutive_healthy_count = 0
            breach_ready = alert.consecutive_breach_count >= max(
                1, settings.node_resource_forecast_breach_consecutive_scans
            )
            if breach_ready:
                _transition_lifecycle(
                    alert,
                    now_naive,
                    quality_ok=True,
                    warning=True,
                    critical=critical,
                    reason=(
                        f"Dự báo {prediction.predicted_percent:.1f}% / upper "
                        f"{prediction.predicted_high if prediction.predicted_high is not None else prediction.predicted_percent:.1f}% "
                        f"sau {alert.consecutive_breach_count} lần breach liên tiếp; "
                        f"{_quality_evidence_summary(prediction)}"
                    ),
                    evidence_version="forecast-consensus:v1",
                )
            else:
                _transition_lifecycle(
                    alert,
                    now_naive,
                    quality_ok=True,
                    candidate=True,
                    reason=(
                        f"Breach {alert.consecutive_breach_count}/"
                        f"{max(1, settings.node_resource_forecast_breach_consecutive_scans)}; "
                        "chưa đủ hysteresis để mở cảnh báo."
                    ),
                    evidence_version="breach-hysteresis:v1",
                )
            alert.resolved_at = None

            cooldown = _alert_cooldown_seconds(critical=critical)
            duplicate_evidence = (
                alert.last_notified_evidence_fingerprint == alert.evidence_fingerprint
            )
            due = (
                alert.last_notified_at is None
                or (now_naive - alert.last_notified_at).total_seconds() >= cooldown
            )
            if due and breach_ready and not duplicate_evidence:
                sent = send_node_forecast_alert(
                    host,
                    metric,
                    prediction.current_percent,
                    prediction.predicted_percent,
                    effective_hours_to_90,
                    prediction.confidence,
                    prediction.samples,
                    window_hours,
                    cluster_name=cluster,
                )
                if sent:
                    alert.last_notified_at = now_naive
                    alert.last_notified_evidence_fingerprint = alert.evidence_fingerprint
                    alert.notification_state = NotificationState.SENT.value
                else:
                    alert.notification_state = NotificationState.FAILED.value
            elif breach_ready:
                alert.notification_state = (
                    NotificationState.SUPPRESSED.value
                    if duplicate_evidence
                    else NotificationState.COOLDOWN.value
                )
            else:
                alert.notification_state = NotificationState.SUPPRESSED.value
        # Keep an append-only history of lifecycle transitions. The mutable
        # alert row is still the operational state; this history is what
        # makes canary alert volume and recovery/early-detection metrics
        # auditable over a 24–72 hour window.
        session.flush()
        current_alerts = session.query(NodeResourceForecastAlert).filter_by(
            cluster_name=cluster, host=host,
        ).all()
        for alert in current_alerts:
            new_state = _legacy_lifecycle_state(alert)
            previous_state = previous_states.get(alert.metric)
            if not new_state or previous_state == new_state:
                continue
            session.add(NodeResourceForecastTransition(
                alert_id=alert.id,
                previous_state=previous_state,
                new_state=new_state,
                reason=alert.state_reason or "lifecycle transition",
                evidence_version=alert.evidence_version,
                changed_at=now_naive,
            ))
            if _forecast_alert_events_table_available(session):
                session.add(NodeResourceForecastAlertEvent(
                    alert_id=alert.id,
                    cluster_name=alert.cluster_name,
                    host=alert.host,
                    metric=alert.metric,
                    from_state=previous_state,
                    to_state=new_state,
                    notification_state=alert.notification_state,
                    reason=alert.state_reason or "lifecycle transition",
                    evidence_fingerprint=alert.evidence_fingerprint,
                    occurred_at=now_naive,
                ))
        session.commit()
