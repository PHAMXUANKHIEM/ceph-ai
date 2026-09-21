"""Persistent, outcome-scored seasonal baselines for individual RBD volumes."""

from __future__ import annotations

import statistics
import json
from collections import defaultdict
from datetime import datetime, timedelta
from shared.time import utc_now

from sqlalchemy import func

from config.settings import settings
from shared import db, telegram_alerts, telegram_outbox
from shared.forecast_consensus import ForecastConsensus, aggregate_forecasts
from shared.forecast_metrics import update_rolling_metrics
from shared.forecast_horizons import parse_horizons
from shared.learning_safety import RateLimiter
from shared.metric_quality import MetricQuality, assess_metric_quality
from shared.models import (
    Cluster,
    VolumeEarlyForecast, VolumeForecastRun, VolumeMetric, VolumeModelState,
    VolumePerfSweep,
)

ALGORITHM = "seasonal_median"
FORECAST_MODEL_VERSION = "seasonal-trend-v1"
METRICS = ("iops", "read_latency_ms", "write_latency_ms")
_last_attempt_bucket: dict[tuple[str, str, str], datetime] = {}
_learning_rate_limiter = RateLimiter()


def _quality_for_points(
    points: list[tuple[datetime, float]],
    observed_at: datetime,
    window_hours: int,
) -> MetricQuality:
    """Apply one quality contract to a volume metric training window."""

    minimum_samples = max(3, settings.volume_learning_min_samples)
    return assess_metric_quality(
        [timestamp for timestamp, _value in points],
        now=observed_at,
        max_age_seconds=max(60, settings.volume_forecast_max_staleness_minutes * 60),
        minimum_samples=minimum_samples,
        expected_interval_seconds=3600,
        minimum_coverage_ratio=settings.volume_learning_min_coverage,
        maximum_gap_seconds=max(3600, settings.volume_learning_max_gap_hours * 3600),
        minimum_history_seconds=min(
            max(0, (minimum_samples - 1) * 3600),
            max(0, window_hours * 3600),
        ),
    )


def _quality_reason(quality: MetricQuality) -> str:
    return f"{quality.status}: {quality.reason}"


def _volume_consensus(values: list[float]) -> ForecastConsensus:
    """Aggregate independent window forecasts with the shared fail-closed rule."""

    return aggregate_forecasts(
        values,
        minimum_candidates=max(1, settings.volume_forecast_min_consensus_candidates),
        minimum_ratio=settings.volume_forecast_min_consensus_ratio,
        absolute_tolerance=max(0.0, settings.volume_forecast_consensus_tolerance_percent),
        relative_tolerance=max(0.0, settings.volume_forecast_consensus_relative_tolerance),
    )


def _candidate_windows() -> list[int]:
    values = set()
    for raw in settings.volume_learning_candidate_hours.split(","):
        try:
            value = int(raw.strip())
        except ValueError:
            continue
        if value >= 24:
            values.add(value)
    return sorted(values) or [24, 72, 168, 720]


def _forecast_horizons() -> list[int]:
    return list(parse_horizons(settings.volume_forecast_horizons))


def _state_for(
    session, cluster_id: str, pool: str, image: str, metric: str,
    window: int, horizon_hours: int = 1,
):
    state = session.query(VolumeModelState).filter_by(
        cluster_id=cluster_id, pool=pool, image=image, metric=metric,
        algorithm=ALGORITHM, window_hours=window, horizon_hours=horizon_hours,
    ).one_or_none()
    if state is None:
        state = VolumeModelState(
            cluster_id=cluster_id, pool=pool, image=image, metric=metric,
            algorithm=ALGORITHM, window_hours=window, horizon_hours=horizon_hours,
        )
        session.add(state)
        session.flush()
    return state


def _evaluate_due(
    session, cluster_id: str, pool: str, image: str,
    actual: dict[str, float], observed_at: datetime,
) -> int:
    due = session.query(VolumeForecastRun).filter_by(
        cluster_id=cluster_id, pool=pool, image=image, status="PENDING",
    ).filter(VolumeForecastRun.target_at <= observed_at).all()
    for run in due:
        actual_value = float(actual[run.metric])
        error = abs(run.predicted_value - actual_value)
        denominator = max(abs(run.predicted_value), abs(actual_value), 1e-6)
        percentage_error = min(100.0, error / denominator * 100.0)
        run.actual_value = actual_value
        run.absolute_error = error
        run.percentage_error = percentage_error
        run.status = "EVALUATED"
        run.evaluated_at = observed_at
        state = _state_for(
            session, cluster_id, pool, image, run.metric, run.window_hours,
            run.horizon_hours,
        )
        old_count = state.evaluated_count
        old_mae = state.mean_absolute_error or 0.0
        old_mape = state.mean_percentage_error or 0.0
        state.evaluated_count = old_count + 1
        state.mean_absolute_error = (old_mae * old_count + error) / state.evaluated_count
        state.mean_percentage_error = (
            old_mape * old_count + percentage_error
        ) / state.evaluated_count
        state.last_absolute_error = error
        state.rolling_metrics_json, rolling = update_rolling_metrics(
            state.rolling_metrics_json,
            run.predicted_value,
            actual_value,
            limit=settings.volume_learning_rolling_samples,
        )
        state.rolling_sample_count = int(rolling["count"])
        state.rolling_mae = float(rolling["mae"])
        state.rolling_rmse = float(rolling["rmse"])
        state.rolling_smape = float(rolling["smape"])
        state.rolling_bias = float(rolling["bias"])
        state.updated_at = observed_at
    return len(due)


def _hourly_history(
    session, cluster_id: str, pool: str, image: str,
    start: datetime, end: datetime,
) -> list[tuple[datetime, dict[str, float]]]:
    """Downsample high-frequency polls to bounded hourly means."""
    buckets: dict[datetime, dict[str, float]] = defaultdict(
        lambda: {"count": 0.0, **{metric: 0.0 for metric in METRICS}}
    )
    rows = session.query(
        VolumeMetric.polled_at, VolumeMetric.iops,
        VolumeMetric.read_latency_ms, VolumeMetric.write_latency_ms,
    ).filter(
        VolumeMetric.cluster_id == cluster_id,
        VolumeMetric.pool == pool,
        VolumeMetric.image == image,
        VolumeMetric.polled_at >= start,
        VolumeMetric.polled_at <= end,
    ).order_by(VolumeMetric.polled_at).yield_per(2000)
    for timestamp, iops, read_latency, write_latency in rows:
        bucket = timestamp.replace(minute=0, second=0, microsecond=0)
        values = buckets[bucket]
        values["count"] += 1
        values["iops"] += float(iops)
        values["read_latency_ms"] += float(read_latency)
        values["write_latency_ms"] += float(write_latency)
    return [
        (timestamp, {
            metric: values[metric] / values["count"] for metric in METRICS
        })
        for timestamp, values in sorted(buckets.items())
    ]


def _baseline(
    points: list[tuple[datetime, float]], target_at: datetime,
) -> tuple[float, float, str, int] | None:
    minimum = max(3, settings.volume_learning_min_samples)
    if len(points) < minimum:
        return None
    same_week_hour = [
        value for timestamp, value in points
        if timestamp.weekday() == target_at.weekday() and timestamp.hour == target_at.hour
    ]
    same_day_hour = [value for timestamp, value in points if timestamp.hour == target_at.hour]
    if len(same_week_hour) >= 3:
        values, seasonal_scope = same_week_hour, "hour_of_week"
    elif len(same_day_hour) >= 3:
        values, seasonal_scope = same_day_hour, "hour_of_day"
    else:
        values, seasonal_scope = [value for _timestamp, value in points], "all_history"
    prediction = float(statistics.median(values))
    deviations = [abs(value - prediction) for value in values]
    mad = float(statistics.median(deviations)) if deviations else 0.0
    stability = max(0.0, 1.0 - mad / max(abs(prediction), 1e-6))
    sample_factor = min(1.0, len(values) / minimum)
    return prediction, round(stability * sample_factor, 6), seasonal_scope, len(values)


def _select_models(
    session, cluster_id: str, pool: str, image: str, metric: str, horizon_hours: int = 1,
) -> None:
    states = session.query(VolumeModelState).filter_by(
        cluster_id=cluster_id, pool=pool, image=image, metric=metric,
        algorithm=ALGORITHM, horizon_hours=horizon_hours,
    ).all()
    if not states:
        return
    eligible = [
        state for state in states
        if state.evaluated_count >= settings.volume_learning_min_outcomes
        and state.mean_absolute_error is not None
    ]
    selected = min(eligible, key=lambda row: row.mean_absolute_error) if eligible else max(
        states, key=lambda row: row.window_hours
    )
    for state in states:
        state.selected = state.id == selected.id


def _robust_hourly_slope(points: list[tuple[datetime, float]]) -> float:
    recent = points[-24:]
    slopes = []
    for (left_at, left), (right_at, right) in zip(recent, recent[1:]):
        hours = (right_at - left_at).total_seconds() / 3600
        if hours > 0:
            slopes.append((right - left) / hours)
    return float(statistics.median(slopes)) if slopes else 0.0


def _selected_window(
    session, cluster_id: str, pool: str, image: str, metric: str, horizon_hours: int = 1,
) -> int:
    state = session.query(VolumeModelState).filter_by(
        cluster_id=cluster_id, pool=pool, image=image, metric=metric,
        algorithm=ALGORITHM, horizon_hours=horizon_hours, selected=True,
    ).one_or_none()
    return state.window_hours if state else max(_candidate_windows())


def _threshold(session, pool: str, metric: str) -> tuple[str | None, float | None]:
    if metric in ("read_latency_ms", "write_latency_ms"):
        return "latency_slo_ms", max(0.0, settings.volume_forecast_latency_slo_ms)
    # A sweep is pool-wide capacity evidence. Do not invent an IOPS ceiling
    # from the largest production sample; without a measured knee we fail closed.
    sweep = session.query(VolumePerfSweep).filter_by(
        pool=pool, status="DONE"
    ).filter(VolumePerfSweep.knee_iops.isnot(None)).order_by(
        VolumePerfSweep.finished_at.desc(), VolumePerfSweep.created_at.desc()
    ).first()
    if sweep is None:
        return None, None
    return "measured_knee_iops", float(sweep.knee_iops) * max(
        0.0, min(1.0, settings.volume_forecast_knee_warning_ratio)
    )


def _record_early_forecasts(
    session, cluster_id: str, pool: str, image: str,
    actual: dict[str, float], observed_at: datetime,
) -> int:
    if not settings.volume_forecast_enabled:
        return 0
    bucket = observed_at.replace(minute=0, second=0, microsecond=0)
    horizons = _forecast_horizons()
    forecast_keys = [
        f"{cluster_id}|{pool}|{image}|{metric}|{horizon}|{FORECAST_MODEL_VERSION}|{bucket.isoformat()}"
        for metric in METRICS for horizon in horizons
    ]
    existing_keys = {
        row[0] for row in session.query(VolumeEarlyForecast.idempotency_key)
        .filter(VolumeEarlyForecast.idempotency_key.in_(forecast_keys)).all()
    }
    if len(existing_keys) == len(forecast_keys):
        return 0
    created = 0
    max_window = min(
        max(_candidate_windows()), max(1, settings.volume_learning_history_days) * 24
    )
    history = _hourly_history(
        session, cluster_id, pool, image,
        observed_at - timedelta(hours=max_window), observed_at,
    )
    source_latest_at = session.query(func.max(VolumeMetric.polled_at)).filter(
        VolumeMetric.cluster_id == cluster_id,
        VolumeMetric.pool == pool,
        VolumeMetric.image == image,
        VolumeMetric.polled_at <= observed_at,
    ).scalar() or (history[-1][0] if history else observed_at)
    for metric in METRICS:
        candidate_rows = []
        quality_rows = []
        for window in _candidate_windows():
            cutoff = observed_at - timedelta(hours=window)
            points = [
                (timestamp, values[metric])
                for timestamp, values in history if timestamp >= cutoff
            ]
            quality = _quality_for_points(points, observed_at, window)
            quality_rows.append((window, quality))
            if not quality.usable:
                continue
            candidate_rows.append((window, points, quality))
        for horizon in horizons:
            key = (
                f"{cluster_id}|{pool}|{image}|{metric}|{horizon}|"
                f"{FORECAST_MODEL_VERSION}|{bucket.isoformat()}"
            )
            if key in existing_keys:
                continue
            target_at = observed_at + timedelta(hours=horizon)
            if not candidate_rows:
                selected_window = _selected_window(session, cluster_id, pool, image, metric, horizon)
                _state_for(session, cluster_id, pool, image, metric, selected_window, horizon)
                selected_quality = next(
                    (quality for window, quality in quality_rows if window == selected_window),
                    quality_rows[-1][1] if quality_rows else assess_metric_quality(
                        [], now=observed_at, max_age_seconds=0, minimum_samples=1,
                        expected_interval_seconds=3600, minimum_coverage_ratio=1.0,
                        maximum_gap_seconds=0,
                    ),
                )
                session.add(VolumeEarlyForecast(
                    cluster_id=cluster_id, pool=pool, image=image, metric=metric,
                    horizon_hours=horizon, generated_at=observed_at, target_at=target_at,
                    source_latest_at=source_latest_at, current_value=actual[metric],
                    predicted_value=actual[metric], threshold_type=None, threshold_value=None,
                    consensus_status="DATA_QUALITY", consensus_ratio=0.0,
                    consensus_candidate_count=0, predicted_low=None, predicted_high=None,
                    model_votes_json="[]",
                    confidence=0.0, training_samples=selected_quality.sample_count,
                    training_window_hours=selected_window,
                    seasonal_scope="none",
                    model_version=FORECAST_MODEL_VERSION, status="DATA_QUALITY",
                    reason=_quality_reason(selected_quality), idempotency_key=key,
                ))
                created += 1
                continue
            candidates = []
            for window, points, quality in candidate_rows:
                baseline = _baseline(points, target_at)
                if baseline is None:
                    continue
                seasonal, baseline_confidence, scope, sample_count = baseline
                predicted = max(0.0, seasonal + _robust_hourly_slope(points) * horizon)
                candidate_confidence = round(
                    baseline_confidence * max(0.5, 1.0 - horizon / 96.0), 6
                )
                candidates.append({
                    "window": window,
                    "predicted": predicted,
                    "confidence": candidate_confidence,
                    "scope": scope,
                    "samples": sample_count,
                })
            if not candidates:
                continue
            consensus = _volume_consensus([row["predicted"] for row in candidates])
            model_votes = json.dumps(candidates, separators=(",", ":"), sort_keys=True)
            preferred_window = _selected_window(session, cluster_id, pool, image, metric, horizon)
            _state_for(session, cluster_id, pool, image, metric, preferred_window, horizon)
            selected = next(
                (row for row in candidates if row["window"] == preferred_window),
                max(candidates, key=lambda row: row["confidence"]),
            )
            predicted = consensus.value
            confidence = round(
                statistics.median(row["confidence"] for row in candidates)
                * consensus.ratio,
                6,
            )
            threshold_type, threshold_value = _threshold(session, pool, metric)
            if not consensus.usable:
                status = "LOW_CONFIDENCE"
                reason = (
                    f"Consensus không đủ: {consensus.agreeing_count}/"
                    f"{consensus.candidate_count} candidate đồng thuận."
                )
            elif confidence < settings.volume_forecast_min_confidence:
                status, reason = "LOW_CONFIDENCE", "Confidence dưới ngưỡng; không phát cảnh báo."
            elif threshold_value is None:
                status, reason = "NO_THRESHOLD", "Chưa có knee IOPS đo được; không suy đoán ngưỡng."
            elif predicted >= threshold_value:
                status, reason = "WARNING", f"Dự báo có thể chạm {threshold_type} trong {horizon} giờ."
            else:
                status, reason = "SAFE", "Dự báo chưa chạm ngưỡng cảnh báo."
            session.add(VolumeEarlyForecast(
                cluster_id=cluster_id, pool=pool, image=image, metric=metric,
                horizon_hours=horizon, generated_at=observed_at, target_at=target_at,
                source_latest_at=source_latest_at, current_value=actual[metric],
                predicted_value=predicted, threshold_type=threshold_type,
                threshold_value=threshold_value, consensus_status=consensus.status,
                consensus_ratio=consensus.ratio,
                consensus_candidate_count=consensus.candidate_count,
                predicted_low=consensus.lower, predicted_high=consensus.upper,
                model_votes_json=model_votes,
                confidence=confidence, training_samples=selected["samples"],
                training_window_hours=selected["window"],
                seasonal_scope=selected["scope"], model_version=FORECAST_MODEL_VERSION,
                status=status, reason=reason, idempotency_key=key,
            ))
            created += 1
    return created


def observe_sample(
    session, cluster_id: str | None, sample: dict, observed_at: datetime,
) -> int:
    """Evaluate due outcomes and record this hour's candidate baselines."""
    if not settings.volume_learning_enabled or not cluster_id:
        return 0
    pool, image = str(sample["pool"]), str(sample["image"])
    # Raw VolumeMetric persistence remains owned by volume_monitor. This gate
    # only bounds the more expensive learning/evaluation work for direct
    # callers and duplicate Watcher ticks; the normal 5-minute cadence passes
    # unchanged with the default interval.
    if not _learning_rate_limiter.allow(
        f"volume:{cluster_id}:{pool}:{image}",
        interval_seconds=settings.learning_job_min_interval_seconds,
        now=observed_at.timestamp(),
    ):
        return 0
    actual = {metric: float(sample[metric]) for metric in METRICS}
    evaluated = _evaluate_due(session, cluster_id, pool, image, actual, observed_at)
    bucket = observed_at.replace(minute=0, second=0, microsecond=0)
    _record_early_forecasts(session, cluster_id, pool, image, actual, observed_at)
    windows = _candidate_windows()
    horizons = _forecast_horizons()
    keys = [
        f"{cluster_id}|{pool}|{image}|{metric}|{ALGORITHM}|{window}|{horizon}|{bucket.isoformat()}"
        for metric in METRICS for window in windows for horizon in horizons
    ]
    existing = {
        row[0] for row in session.query(VolumeForecastRun.idempotency_key)
        .filter(VolumeForecastRun.idempotency_key.in_(keys)).all()
    }
    if len(existing) == len(keys):
        return evaluated
    attempt_key = (cluster_id, pool, image)
    if _last_attempt_bucket.get(attempt_key) == bucket:
        return evaluated
    # Avoid re-reading up to 30 days of history every 15-second poll while a
    # new volume is still warming up and therefore has no candidate row yet.
    _last_attempt_bucket[attempt_key] = bucket

    max_history = min(max(windows), max(1, settings.volume_learning_history_days) * 24)
    history = _hourly_history(
        session, cluster_id, pool, image,
        observed_at - timedelta(hours=max_history), observed_at,
    )
    for metric in METRICS:
        for horizon in horizons:
            target_at = observed_at + timedelta(hours=horizon)
            for window in windows:
                key = f"{cluster_id}|{pool}|{image}|{metric}|{ALGORITHM}|{window}|{horizon}|{bucket.isoformat()}"
                if key in existing:
                    continue
                cutoff = observed_at - timedelta(hours=window)
                points = [(timestamp, values[metric]) for timestamp, values in history if timestamp >= cutoff]
                if not _quality_for_points(points, observed_at, window).usable:
                    continue
                baseline = _baseline(points, target_at)
                if baseline is None:
                    continue
                prediction, confidence, seasonal_scope, training_samples = baseline
                session.add(VolumeForecastRun(
                    cluster_id=cluster_id, pool=pool, image=image, metric=metric,
                    algorithm=ALGORITHM, window_hours=window, horizon_hours=horizon,
                    predicted_at=observed_at, target_at=target_at, current_value=actual[metric],
                    predicted_value=prediction, confidence=confidence,
                    seasonal_scope=seasonal_scope, training_samples=training_samples,
                    status="PENDING", idempotency_key=key,
                ))
            _select_models(session, cluster_id, pool, image, metric, horizon)
    return evaluated


def deliver_pending_forecast_alerts(cluster_id: str | None) -> int:
    """Best-effort delivery of unsent WARNING rows; mark only after success."""
    if not cluster_id:
        return 0
    delivered = 0
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(id=cluster_id).one_or_none()
        if cluster is None:
            return 0
        rows = session.query(VolumeEarlyForecast).filter_by(
            cluster_id=cluster_id, status="WARNING", telegram_sent_at=None,
        ).order_by(VolumeEarlyForecast.created_at).limit(50).all()
        for row in rows:
            sent = telegram_outbox.enqueue_alert_call_and_dispatch(
                event_id=f"volume-forecast:{row.id}",
                category="rbd-forecast",
                function="send_volume_forecast_alert",
                kwargs={
                    "pool": row.pool,
                    "image": row.image,
                    "metric": row.metric,
                    "horizon_hours": row.horizon_hours,
                    "current_value": row.current_value,
                    "predicted_value": row.predicted_value,
                    "threshold_type": row.threshold_type,
                    "threshold_value": row.threshold_value,
                    "confidence": row.confidence,
                    "training_samples": row.training_samples,
                    "training_window_hours": row.training_window_hours,
                    "model_version": row.model_version,
                    "target_at": row.target_at.isoformat(),
                },
                cluster_id=cluster_id,
                cluster_name=cluster.name,
                sender=telegram_alerts.send_volume_forecast_alert,
            )
            if sent:
                row.telegram_sent_at = utc_now()
                delivered += 1
        session.commit()
    return delivered
