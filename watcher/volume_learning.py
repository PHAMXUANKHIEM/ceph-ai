"""Persistent, outcome-scored seasonal baselines for individual RBD volumes."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from config.settings import settings
from shared.models import VolumeForecastRun, VolumeMetric, VolumeModelState

ALGORITHM = "seasonal_median"
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
