"""Deterministic Ceph cluster/pool/OSD capacity history and forecasting."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from shared.time import utc_now
from math import sqrt
from statistics import median
import uuid

from sqlalchemy import and_, func
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import db
from shared.models import CapacityAlertState, CephCapacitySample, Cluster
from shared.telegram_alerts import send_capacity_recovery_alert, send_capacity_threshold_alert
from watcher.capacity_evidence import _cluster_stats, _osd_stats, _pool_stats, _query


@dataclass(frozen=True)
class Forecast:
    entity_type: str
    entity_name: str
    current_percent: float
    growth_percent_per_day: float
    confidence: float
    sample_count: int
    history_days: float
    thresholds: dict[str, str | None]
    additional_bytes_at_95: int
    forecast_method: str = "linear"
    confidence_interval: dict[str, float] = field(default_factory=dict)
    predicted_percent_at_horizon: float | None = None
    spike_detected: bool = False
    risk_explanation: tuple[str, ...] = ()
    backtest: dict[str, float | int | None] = field(default_factory=dict)


CAPACITY_THRESHOLDS = (80, 90, 95)
ALERT_RETRY_SECONDS = 300


def _reached_threshold(percent: float) -> int | None:
    return next((threshold for threshold in reversed(CAPACITY_THRESHOLDS) if percent >= threshold), None)


def _deliver_transition(
    cluster_id: str, cluster_name: str | None, kind: str, name: str, row: dict,
    previous_percent: float | None, now: datetime,
) -> None:
    """Claim and deliver one transition; failed deliveries remain retryable."""
    current = _reached_threshold(float(row["used_percent"])) or 0
    previous = _reached_threshold(previous_percent or 0.0) or 0
    with db.SessionLocal() as session:
        state = session.query(CapacityAlertState).filter_by(
            cluster_id=cluster_id, entity_type=kind, entity_name=name,
        ).one_or_none()
        if state is None:
            # Existing series are bootstrapped as already notified so a
            # deployment does not replay historical thresholds.
            state = CapacityAlertState(
                id=str(uuid.uuid4()), cluster_id=cluster_id, entity_type=kind,
                entity_name=name, current_threshold=current,
                notified_threshold=previous if previous_percent is not None else 0,
                updated_at=now,
            )
            session.add(state)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                state = session.query(CapacityAlertState).filter_by(
                    cluster_id=cluster_id, entity_type=kind, entity_name=name,
                ).one()
        if state.current_threshold != current:
            state.current_threshold = current
            state.updated_at = now
            state.last_attempt_at = None
            session.commit()
        notified = state.notified_threshold
        retry_before = now - timedelta(seconds=ALERT_RETRY_SECONDS)
        claimed = session.query(CapacityAlertState).filter(
            CapacityAlertState.id == state.id,
            CapacityAlertState.current_threshold == current,
            CapacityAlertState.notified_threshold == notified,
            CapacityAlertState.current_threshold != CapacityAlertState.notified_threshold,
            (CapacityAlertState.last_attempt_at.is_(None))
            | (CapacityAlertState.last_attempt_at <= retry_before),
        ).update({CapacityAlertState.last_attempt_at: now}, synchronize_session=False)
        session.commit()
        if not claimed:
            return
        state_id = state.id

    if current > notified:
        delivered = send_capacity_threshold_alert(
            kind, name, float(row["used_percent"]), current,
            int(row["used_bytes"]), int(row["total_bytes"]), cluster_name=cluster_name,
        )
    else:
        delivered = send_capacity_recovery_alert(
            kind, name, float(row["used_percent"]), notified, current,
            cluster_name=cluster_name,
        )
    if delivered:
        with db.SessionLocal() as session:
            session.query(CapacityAlertState).filter(
                CapacityAlertState.id == state_id,
                CapacityAlertState.current_threshold == current,
            ).update({
                CapacityAlertState.notified_threshold: current,
                CapacityAlertState.updated_at: now,
            }, synchronize_session=False)
            session.commit()


def collect_and_store(cluster_id: str, cluster: Cluster | None = None, *, now: datetime | None = None) -> int:
    """Collect one coherent capacity tick. Failed queries write no partial tick."""
    captured_at = now or utc_now()
    df = _query(cluster, "ceph df detail")
    osd_df = _query(cluster, "ceph osd df")
    cluster_row = _cluster_stats(df)
    rows = [("cluster", "cluster", cluster_row)]
    for row in _pool_stats(df, limit=None):
        total = row["used_bytes"] + row["max_available_bytes"]
        rows.append(("pool", row["pool"], {**row, "total_bytes": total}))
    for row in _osd_stats(osd_df, limit=None):
        rows.append(("osd", f"osd.{row['osd_id']}", {
            **row, "used_bytes": row["used_kb"] * 1024, "total_bytes": row["total_kb"] * 1024,
        }))
    valid = [(kind, name, row) for kind, name, row in rows if row.get("total_bytes", 0) > 0]
    with db.SessionLocal() as session:
        latest_times = session.query(
            CephCapacitySample.entity_type,
            CephCapacitySample.entity_name,
            func.max(CephCapacitySample.captured_at).label("captured_at"),
        ).filter(CephCapacitySample.cluster_id == cluster_id).group_by(
            CephCapacitySample.entity_type, CephCapacitySample.entity_name,
        ).subquery()
        previous_rows = session.query(CephCapacitySample).join(
            latest_times,
            and_(
                CephCapacitySample.entity_type == latest_times.c.entity_type,
                CephCapacitySample.entity_name == latest_times.c.entity_name,
                CephCapacitySample.captured_at == latest_times.c.captured_at,
            ),
        ).filter(CephCapacitySample.cluster_id == cluster_id).all()
        previous = {
            (row.entity_type, row.entity_name): row.used_percent for row in previous_rows
        }
        session.add_all([CephCapacitySample(
            cluster_id=cluster_id, entity_type=kind, entity_name=name,
            used_bytes=int(row["used_bytes"]), total_bytes=int(row["total_bytes"]),
            used_percent=float(row["used_percent"]), captured_at=captured_at,
        ) for kind, name, row in valid])
        session.commit()
        cluster_name = cluster.name if cluster is not None else session.query(Cluster.name).filter_by(id=cluster_id).scalar()

    for kind, name, row in valid:
        _deliver_transition(
            cluster_id, cluster_name, kind, name, row,
            previous.get((kind, name)), captured_at,
        )
    return len(valid)


def _fit_model(rows: list[CephCapacitySample]) -> dict | None:
    """Fit a bounded trend plus optional weekly residual correction.

    This is deliberately deterministic and dependency-free.  A weekly
    correction is only enabled after two weeks of history and at least two
    observations for every participating weekday, so a short or sparse series
    cannot manufacture seasonality.
    """

    if len(rows) < 2:
        return None
    origin = rows[0].captured_at
    xs = [(row.captured_at - origin).total_seconds() / 86400 for row in rows]
    ys = [float(row.used_percent) for row in rows]
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom if denom else 0.0
    intercept = y_mean - slope * x_mean
    fitted = [intercept + slope * x for x in xs]
    residuals = [y - fit for y, fit in zip(ys, fitted)]

    weekday_values: dict[int, list[float]] = {}
    if xs[-1] - xs[0] >= 14:
        for row, residual in zip(rows, residuals):
            weekday_values.setdefault(row.captured_at.weekday(), []).append(residual)
    seasonal = {
        weekday: sum(values) / len(values)
        for weekday, values in weekday_values.items()
        if len(values) >= 2
    }
    # Center the correction so it does not alter the long-term trend level.
    if seasonal:
        correction_mean = sum(seasonal.values()) / len(seasonal)
        seasonal = {weekday: value - correction_mean for weekday, value in seasonal.items()}
        if max(abs(value) for value in seasonal.values()) < 0.5:
            # A perfectly linear series has one or two samples for every
            # weekday too, but a zero-amplitude correction is not seasonality.
            seasonal = {}

    total_var = sum((y - y_mean) ** 2 for y in ys)
    residual = sum(value * value for value in residuals)
    r_squared = max(0.0, min(1.0, 1 - residual / total_var)) if total_var else 0.0
    delta_values = [ys[index] - ys[index - 1] for index in range(1, len(ys))]
    typical_delta = median([abs(value) for value in delta_values]) if delta_values else 0.0
    latest_delta = delta_values[-1] if delta_values else 0.0
    spike_detected = len(delta_values) >= 5 and abs(latest_delta) > max(2.0, typical_delta * 3)
    confidence = max(0.0, min(1.0, r_squared - (0.25 if spike_detected else 0.0)))
    method = "seasonal_linear" if seasonal else "linear"
    if spike_detected:
        method = f"{method}_spike_guarded"
    return {
        "origin": origin,
        "xs": xs,
        "ys": ys,
        "slope": slope,
        "intercept": intercept,
        "seasonal": seasonal,
        "confidence": confidence,
        "r_squared": r_squared,
        "spike_detected": spike_detected,
        "method": method,
        "residual_std": sqrt(residual / max(1, len(rows) - 2)),
        "x_mean": x_mean,
        "denom": denom,
    }


def _predict(model: dict, at: datetime) -> float:
    days = (at - model["origin"]).total_seconds() / 86400
    seasonal = model["seasonal"].get(at.weekday(), 0.0)
    return model["intercept"] + model["slope"] * days + seasonal


def _confidence_interval(model: dict, at: datetime) -> dict[str, float]:
    days = (at - model["origin"]).total_seconds() / 86400
    n = len(model["ys"])
    leverage = 1 + (1 / n) + ((days - model["x_mean"]) ** 2 / model["denom"] if model["denom"] else 0.0)
    margin = 1.96 * model["residual_std"] * sqrt(max(1.0, leverage))
    prediction = _predict(model, at)
    return {
        "low": round(max(0.0, min(100.0, prediction - margin)), 3),
        "high": round(max(0.0, min(100.0, prediction + margin)), 3),
    }


def backtest(rows: list[CephCapacitySample], *, horizon_days: int = 7) -> dict[str, float | int | None]:
    """Evaluate rolling one-step/short-horizon predictions without mutation."""

    horizon_days = max(1, int(horizon_days))
    minimum = max(5, min(settings.capacity_forecast_min_samples, len(rows) - horizon_days))
    errors: list[float] = []
    percentage_errors: list[float] = []
    for split in range(minimum, len(rows) - horizon_days + 1):
        model = _fit_model(rows[:split])
        if model is None:
            continue
        target = rows[split + horizon_days - 1]
        prediction = _predict(model, target.captured_at)
        actual = float(target.used_percent)
        errors.append(abs(prediction - actual))
        if actual:
            percentage_errors.append(abs(prediction - actual) / abs(actual) * 100)
    if not errors:
        return {"status": "insufficient_history", "samples": 0, "mae": None, "mape": None}
    return {
        "status": "ready",
        "samples": len(errors),
        "mae": round(sum(errors) / len(errors), 4),
        "mape": round(sum(percentage_errors) / len(percentage_errors), 4) if percentage_errors else None,
    }


def _forecast(rows: list[CephCapacitySample], now: datetime, *, include_backtest: bool = True) -> Forecast | None:
    if len(rows) < settings.capacity_forecast_min_samples:
        return None
    model = _fit_model(rows)
    if model is None:
        return None
    origin = model["origin"]
    span_days = (rows[-1].captured_at - origin).total_seconds() / 86400
    if span_days < settings.capacity_forecast_min_history_days:
        return None
    current = float(rows[-1].used_percent)
    confidence = model["confidence"]
    threshold_dates: dict[str, str | None] = {}
    for threshold in (80, 90, 95):
        days = 0.0 if current >= threshold else -1
        if days < 0 and model["slope"] > 0:
            for offset in range(1, settings.capacity_forecast_horizon_days + 1):
                projected_at = now + timedelta(days=offset)
                if _predict(model, projected_at) >= threshold:
                    days = float(offset)
                    break
        threshold_dates[str(threshold)] = (
            (now + timedelta(days=days)).date().isoformat()
            if 0 <= days <= settings.capacity_forecast_horizon_days and confidence >= settings.capacity_forecast_min_confidence
            else None
        )
    latest = rows[-1]
    projected_used_at_95 = latest.total_bytes * .95
    additional = max(0, int(projected_used_at_95 / .8 - latest.total_bytes))
    horizon_at = now + timedelta(days=settings.capacity_forecast_horizon_days)
    explanation = []
    if rows[-1].entity_type == "cluster":
        explanation.append("Toàn cụm là tổng capacity raw; replica/EC overhead chưa được tách riêng trong nguồn hiện tại.")
    elif rows[-1].entity_type == "pool":
        explanation.append(f"Pool {rows[-1].entity_name} được xếp theo phần trăm dung lượng quan sát được.")
    else:
        explanation.append(f"{rows[-1].entity_name} được xếp theo dung lượng OSD quan sát được.")
    if model["slope"] > 0:
        explanation.append(f"Xu hướng tăng khoảng {model['slope']:.3f} điểm phần trăm/ngày.")
    elif model["slope"] < 0:
        explanation.append(f"Xu hướng đang giảm khoảng {abs(model['slope']):.3f} điểm phần trăm/ngày.")
    if model["spike_detected"]:
        explanation.append("Có spike ở mẫu gần nhất; confidence đã bị giảm và cần kiểm tra workload thực tế.")
    return Forecast(
        rows[-1].entity_type, rows[-1].entity_name, round(current, 3), round(model["slope"], 4),
        round(confidence, 4), len(rows), round(span_days, 2), threshold_dates, additional,
        forecast_method=model["method"], confidence_interval=_confidence_interval(model, horizon_at),
        predicted_percent_at_horizon=round(max(0.0, min(100.0, _predict(model, horizon_at))), 3),
        spike_detected=model["spike_detected"], risk_explanation=tuple(explanation),
        backtest=backtest(rows) if include_backtest else {},
    )


def forecasts(cluster_id: str, *, now: datetime | None = None) -> dict:
    now = now or utc_now()
    cutoff = now - timedelta(days=settings.capacity_forecast_history_days)
    with db.SessionLocal() as session:
        samples = session.query(CephCapacitySample).filter(
            CephCapacitySample.cluster_id == cluster_id, CephCapacitySample.captured_at >= cutoff
        ).order_by(CephCapacitySample.entity_type, CephCapacitySample.entity_name, CephCapacitySample.captured_at).all()
    grouped: dict[tuple[str, str], list] = {}
    for row in samples:
        grouped.setdefault((row.entity_type, row.entity_name), []).append(row)
    ready = [value for rows in grouped.values() if (value := _forecast(rows, now)) is not None]
    return {
        "status": "ready" if ready else "insufficient_history",
        "minimum_history_days": settings.capacity_forecast_min_history_days,
        "minimum_samples": settings.capacity_forecast_min_samples,
        "series_seen": len(grouped),
        "forecasts": [asdict(value) for value in ready],
    }
