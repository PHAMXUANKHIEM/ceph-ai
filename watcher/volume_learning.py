"""Persistent, outcome-scored seasonal baselines for individual RBD volumes."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

from config.settings import settings
from shared.models import (
    VolumeEarlyForecast, VolumeForecastRun, VolumeMetric, VolumeModelState,
    VolumePerfSweep,
)

ALGORITHM = "seasonal_median"
FORECAST_MODEL_VERSION = "seasonal-trend-v1"
METRICS = ("iops", "read_latency_ms", "write_latency_ms")
_last_attempt_bucket: dict[tuple[str, str, str], datetime] = {}


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
    values = set()
    for raw in settings.volume_forecast_horizons.split(","):
        try:
            value = int(raw.strip())
        except ValueError:
            continue
        if value > 0:
            values.add(value)
    return sorted(values) or [1, 6, 24]


def _state_for(session, cluster_id: str, pool: str, image: str, metric: str, window: int):
    state = session.query(VolumeModelState).filter_by(
        cluster_id=cluster_id, pool=pool, image=image, metric=metric,
        algorithm=ALGORITHM, window_hours=window,
    ).one_or_none()
    if state is None:
        state = VolumeModelState(
            cluster_id=cluster_id, pool=pool, image=image, metric=metric,
            algorithm=ALGORITHM, window_hours=window,
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
            session, cluster_id, pool, image, run.metric, run.window_hours
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


def _select_models(session, cluster_id: str, pool: str, image: str, metric: str) -> None:
    states = session.query(VolumeModelState).filter_by(
        cluster_id=cluster_id, pool=pool, image=image, metric=metric,
        algorithm=ALGORITHM,
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


def _selected_window(session, cluster_id: str, pool: str, image: str, metric: str) -> int:
    state = session.query(VolumeModelState).filter_by(
        cluster_id=cluster_id, pool=pool, image=image, metric=metric,
        algorithm=ALGORITHM, selected=True,
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
    if not history:
        return 0
    source_latest_at = session.query(func.max(VolumeMetric.polled_at)).filter(
        VolumeMetric.cluster_id == cluster_id,
        VolumeMetric.pool == pool,
        VolumeMetric.image == image,
        VolumeMetric.polled_at <= observed_at,
    ).scalar() or history[-1][0]
    stale = observed_at - source_latest_at > timedelta(
        minutes=max(1, settings.volume_forecast_max_staleness_minutes)
    )
    for metric in METRICS:
        window = _selected_window(session, cluster_id, pool, image, metric)
        cutoff = observed_at - timedelta(hours=window)
        points = [(timestamp, values[metric]) for timestamp, values in history if timestamp >= cutoff]
        for horizon in horizons:
            key = (
                f"{cluster_id}|{pool}|{image}|{metric}|{horizon}|"
                f"{FORECAST_MODEL_VERSION}|{bucket.isoformat()}"
            )
            if key in existing_keys:
                continue
            target_at = observed_at + timedelta(hours=horizon)
            baseline = _baseline(points, target_at)
            if baseline is None:
                continue
            seasonal, baseline_confidence, scope, sample_count = baseline
            predicted = max(0.0, seasonal + _robust_hourly_slope(points) * horizon)
            confidence = round(
                baseline_confidence * max(0.5, 1.0 - horizon / 96.0), 6
            )
            threshold_type, threshold_value = _threshold(session, pool, metric)
            if stale:
                status, reason = "STALE", "Mẫu mới nhất đã quá hạn; không phát cảnh báo."
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
                threshold_value=threshold_value, confidence=confidence,
                training_samples=sample_count, training_window_hours=window,
                seasonal_scope=scope, model_version=FORECAST_MODEL_VERSION,
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
    actual = {metric: float(sample[metric]) for metric in METRICS}
    evaluated = _evaluate_due(session, cluster_id, pool, image, actual, observed_at)
    bucket = observed_at.replace(minute=0, second=0, microsecond=0)
    _record_early_forecasts(session, cluster_id, pool, image, actual, observed_at)
    windows = _candidate_windows()
    keys = [
        f"{cluster_id}|{pool}|{image}|{metric}|{ALGORITHM}|{window}|{bucket.isoformat()}"
        for metric in METRICS for window in windows
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
    target_at = observed_at + timedelta(hours=max(1, settings.volume_learning_evaluation_hours))
    for metric in METRICS:
        for window in windows:
            key = f"{cluster_id}|{pool}|{image}|{metric}|{ALGORITHM}|{window}|{bucket.isoformat()}"
            if key in existing:
                continue
            cutoff = observed_at - timedelta(hours=window)
            points = [(timestamp, values[metric]) for timestamp, values in history if timestamp >= cutoff]
            baseline = _baseline(points, target_at)
            if baseline is None:
                continue
            prediction, confidence, seasonal_scope, training_samples = baseline
            session.add(VolumeForecastRun(
                cluster_id=cluster_id, pool=pool, image=image, metric=metric,
                algorithm=ALGORITHM, window_hours=window, predicted_at=observed_at,
                target_at=target_at, current_value=actual[metric],
                predicted_value=prediction, confidence=confidence,
                seasonal_scope=seasonal_scope, training_samples=training_samples,
                status="PENDING", idempotency_key=key,
            ))
        _select_models(session, cluster_id, pool, image, metric)
    return evaluated
