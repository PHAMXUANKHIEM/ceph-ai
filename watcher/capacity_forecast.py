"""Deterministic Ceph cluster/pool/OSD capacity history and forecasting."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func

from config.settings import settings
from shared import db
from shared.models import CephCapacitySample, Cluster
from shared.telegram_alerts import send_capacity_threshold_alert
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


CAPACITY_THRESHOLDS = (80, 90, 95)


def _reached_threshold(percent: float) -> int | None:
    return next((threshold for threshold in reversed(CAPACITY_THRESHOLDS) if percent >= threshold), None)


def collect_and_store(cluster_id: str, cluster: Cluster | None = None, *, now: datetime | None = None) -> int:
    """Collect one coherent capacity tick. Failed queries write no partial tick."""
    captured_at = now or datetime.utcnow()
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
        current_percent = float(row["used_percent"])
        current_threshold = _reached_threshold(current_percent)
        previous_threshold = _reached_threshold(previous.get((kind, name), 0.0))
        if current_threshold is not None and (
            previous_threshold is None or current_threshold > previous_threshold
        ):
            send_capacity_threshold_alert(
                kind, name, current_percent, current_threshold,
                int(row["used_bytes"]), int(row["total_bytes"]),
                cluster_name=cluster_name,
            )
    return len(valid)


def _forecast(rows: list[CephCapacitySample], now: datetime) -> Forecast | None:
    if len(rows) < settings.capacity_forecast_min_samples:
        return None
    origin = rows[0].captured_at
    span_days = (rows[-1].captured_at - origin).total_seconds() / 86400
    if span_days < settings.capacity_forecast_min_history_days:
        return None
    xs = [(row.captured_at - origin).total_seconds() / 86400 for row in rows]
    ys = [row.used_percent for row in rows]
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom if denom else 0.0
    fitted = [y_mean + slope * (x - x_mean) for x in xs]
    total_var = sum((y - y_mean) ** 2 for y in ys)
    residual = sum((y - fit) ** 2 for y, fit in zip(ys, fitted))
    confidence = max(0.0, min(1.0, 1 - residual / total_var)) if total_var else 0.0
    current = ys[-1]
    threshold_dates: dict[str, str | None] = {}
    for threshold in (80, 90, 95):
        days = 0.0 if current >= threshold else ((threshold - current) / slope if slope > 0 else -1)
        threshold_dates[str(threshold)] = (
            (now + timedelta(days=days)).date().isoformat()
            if 0 <= days <= settings.capacity_forecast_horizon_days and confidence >= settings.capacity_forecast_min_confidence
            else None
        )
    latest = rows[-1]
    projected_used_at_95 = latest.total_bytes * .95
    additional = max(0, int(projected_used_at_95 / .8 - latest.total_bytes))
    return Forecast(rows[-1].entity_type, rows[-1].entity_name, round(current, 3), round(slope, 4),
                    round(confidence, 4), len(rows), round(span_days, 2), threshold_dates, additional)


def forecasts(cluster_id: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.utcnow()
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
