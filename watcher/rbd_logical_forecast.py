"""Read-only logical RBD forecast; never confused with physical Ceph capacity."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from shared import db
from shared.models import RbdCapacitySample

MAX_POOLS = 4
MAX_POINTS = 500
HORIZON_DAYS = 7
MIN_POINTS = 7
MIN_SPAN_DAYS = 2
MAX_AGE = timedelta(hours=2)
METRICS = ("provisioned_bytes", "head_used_bytes", "snapshot_used_bytes")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def fit_logical_series(rows: list[RbdCapacitySample], metric: str, *, now: datetime) -> dict:
    """Fit a bounded linear candidate only with fresh, complete observations."""
    if metric not in METRICS:
        raise ValueError("unsupported logical RBD metric")
    ordered = sorted(rows, key=lambda row: _utc(row.captured_at))[-MAX_POINTS:]
    values: list[tuple[datetime, float]] = []
    for row in ordered:
        raw = getattr(row, metric)
        if raw is None:
            continue
        value = float(raw)
        if math.isfinite(value) and value >= 0 and _utc(row.captured_at) <= _utc(now):
            values.append((_utc(row.captured_at), value))
    result = {"metric": metric, "status": "INSUFFICIENT_EVIDENCE", "sample_count": len(values),
              "horizon_days": HORIZON_DAYS, "prediction_bytes": None,
              "growth_bytes_per_day": None, "confidence": None,
              "reason": "insufficient history or missing usage"}
    if len(values) < MIN_POINTS or len(values) != len(ordered):
        return result
    span = (values[-1][0] - values[0][0]).total_seconds() / 86400
    if span < MIN_SPAN_DAYS:
        return {**result, "reason": "history span is too short"}
    age = _utc(now) - values[-1][0]
    if age < timedelta(0) or age > MAX_AGE:
        return {**result, "status": "STALE", "reason": "latest RBD observation is stale"}
    xs = [(stamp - values[0][0]).total_seconds() / 86400 for stamp, _ in values]
    ys = [value for _, value in values]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator <= 0:
        return {**result, "reason": "duplicate timestamps"}
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    intercept = mean_y - slope * mean_x
    residual = sum((y - intercept - slope * x) ** 2 for x, y in zip(xs, ys))
    variance = sum((y - mean_y) ** 2 for y in ys)
    confidence = max(0.0, min(1.0, 1 - residual / variance)) if variance > 0 else 0.0
    target = xs[-1] + HORIZON_DAYS
    prediction = max(0, int(round(intercept + slope * target)))
    residual_std = math.sqrt(residual / max(1, len(xs) - 2))
    leverage = 1 + 1 / len(xs) + (target - mean_x) ** 2 / denominator
    margin = 1.96 * residual_std * math.sqrt(leverage)
    stride = max(1, math.ceil(len(values) / 24))
    history = [{"at": stamp.isoformat(), "bytes": int(value)} for stamp, value in values[::stride]]
    if history[-1]["at"] != values[-1][0].isoformat():
        history.append({"at": values[-1][0].isoformat(), "bytes": int(values[-1][1])})
    return {**result, "status": "ADVISORY", "reason": "logical RBD trend only; physical/raw capacity unchanged",
            "current_bytes": int(ys[-1]), "prediction_bytes": prediction,
            "growth_bytes_per_day": int(round(slope)), "confidence": round(confidence, 4),
            "observed_at": values[-1][0].isoformat(), "history_days": round(span, 3),
            "prediction_at": (values[-1][0] + timedelta(days=HORIZON_DAYS)).isoformat(),
            "prediction_low_bytes": max(0, int(round(prediction - margin))),
            "prediction_high_bytes": max(0, int(round(prediction + margin))),
            "history": history}


def logical_forecasts(cluster_id: str, *, now: datetime | None = None) -> dict:
    """Return bounded pool-scoped candidate trends without alerts or mutations."""
    now = _utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=90)
    with db.SessionLocal() as session:
        pool_names = [name for (name,) in session.query(RbdCapacitySample.pool).filter(
            RbdCapacitySample.cluster_id == cluster_id,
            RbdCapacitySample.captured_at >= cutoff.replace(tzinfo=None),
            RbdCapacitySample.captured_at <= now.replace(tzinfo=None),
        ).distinct().order_by(RbdCapacitySample.pool).limit(MAX_POOLS + 1).all()]
        truncated = len(pool_names) > MAX_POOLS
        pools = []
        for pool in pool_names[:MAX_POOLS]:
            rows = session.query(RbdCapacitySample).filter(
                RbdCapacitySample.cluster_id == cluster_id, RbdCapacitySample.pool == pool,
                RbdCapacitySample.captured_at >= cutoff.replace(tzinfo=None),
                RbdCapacitySample.captured_at <= now.replace(tzinfo=None),
            ).order_by(RbdCapacitySample.captured_at.desc()).limit(MAX_POINTS).all()
            pools.append({"pool": pool, "series": [fit_logical_series(rows, metric, now=now)
                                                  for metric in METRICS]})
    return {"cluster_id": cluster_id, "execution_mode": "ADVISORY", "source": "rbd_capacity_samples",
            "scope": "configured_rbd_pools_only", "truncated": truncated, "pools": pools}
