"""Robust dynamic baselines for Vitastor cluster, OSD, pool and image metrics."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta

from config.settings import settings
from shared import db
from shared.models import VitastorAnomalyEvent, VitastorEntityMetricSample


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _latency_ms(value) -> float:
    return _number(value) / 1000


def _direction(stats: dict, name: str) -> dict:
    value = stats.get(name) or stats.get(f"primary_{name}") or {}
    return value if isinstance(value, dict) else {}


def extract_entities(datasets: dict, summary: dict) -> list[dict]:
    """Normalize current telemetry without carrying credentials or connection data."""
    summary_io = summary.get("io") or {}
    read, write = _direction(summary_io, "read"), _direction(summary_io, "write")
    entities = [{"type": "cluster", "name": "cluster", "metrics": {
        "read_latency_ms": _latency_ms(read.get("lat")), "write_latency_ms": _latency_ms(write.get("lat")),
        "read_iops": _number(read.get("iops")), "write_iops": _number(write.get("iops")),
        "read_bps": _number(read.get("bps")), "write_bps": _number(write.get("bps")),
    }}]
    for item in datasets.get("osds") or []:
        if not isinstance(item, dict) or item.get("type") != "osd": continue
        io = item.get("op_stats") if isinstance(item.get("op_stats"), dict) else {}
        r, w = _direction(io, "read"), _direction(io, "write")
        entities.append({"type": "osd", "name": str(item.get("name") or item.get("id") or "unknown"), "metrics": {
            "read_latency_ms": _latency_ms(r.get("lat")), "write_latency_ms": _latency_ms(w.get("lat")),
            "read_iops": _number(r.get("iops")), "write_iops": _number(w.get("iops")),
            "read_bps": _number(r.get("bps")), "write_bps": _number(w.get("bps")),
        }})
    for entity_type, rows in (("pool", datasets.get("pools") or []), ("image", datasets.get("images") or [])):
        for item in rows:
            if not isinstance(item, dict): continue
            name = str(item.get("name") or item.get("id") or "unknown")
            if entity_type == "image" and item.get("pool_name"): name = f"{item['pool_name']}/{name}"
            entities.append({"type": entity_type, "name": name, "metrics": {
                "read_latency_ms": _latency_ms(item.get("read_lat")), "write_latency_ms": _latency_ms(item.get("write_lat")),
                "read_iops": _number(item.get("read_iops")), "write_iops": _number(item.get("write_iops")),
                "read_bps": _number(item.get("read_bps")), "write_bps": _number(item.get("write_bps")),
            }})
    return entities


def robust_baseline(values: list[float]) -> tuple[float, float]:
    median = statistics.median(values)
    mad = statistics.median(abs(value - median) for value in values)
    return median, mad * 1.4826


def is_anomaly(metric: str, current: float, values: list[float]) -> tuple[bool, float, float]:
    baseline, sigma = robust_baseline(values)
    if metric.endswith("latency_ms"):
        floor = 0.5
    else:
        floor = 100.0 if metric.endswith("iops") else 1024 * 1024
        if baseline <= 0: return False, baseline, 0.0
    threshold = max(baseline * settings.vitastor_anomaly_relative_multiplier, baseline + settings.vitastor_anomaly_mad_multiplier * sigma, baseline + floor)
    ratio = current / baseline if baseline > 0 else current / floor
    return current > threshold, baseline, ratio


def detect_and_record(cluster_id: str, entities: list[dict], now: datetime | None = None) -> dict:
    """Detect against prior samples, persist lifecycle, then append current samples."""
    now = now or datetime.utcnow(); opened = []; resolved = []
    with db.SessionLocal() as session:
        for entity in entities:
            rows = session.query(VitastorEntityMetricSample).filter_by(
                cluster_id=cluster_id, entity_type=entity["type"], entity_name=entity["name"],
            ).order_by(VitastorEntityMetricSample.collected_at.desc()).limit(settings.vitastor_anomaly_history_samples).all()
            same_hour = [row for row in rows if min((row.collected_at.hour-now.hour)%24, (now.hour-row.collected_at.hour)%24) <= 1]
            baseline_rows = same_hour if len(same_hour) >= settings.vitastor_anomaly_min_samples else rows
            histories = [json.loads(row.metrics_json) for row in baseline_rows]
            for metric, current in entity["metrics"].items():
                values = [_number(item.get(metric)) for item in histories if isinstance(item.get(metric), (int, float))]
                if len(values) < settings.vitastor_anomaly_min_samples: continue
                abnormal, baseline, ratio = is_anomaly(metric, _number(current), values)
                key = (entity["type"], entity["name"], metric)
                event = session.query(VitastorAnomalyEvent).filter_by(cluster_id=cluster_id, entity_type=key[0], entity_name=key[1], metric=key[2], status="OPEN").first()
                if abnormal:
                    severity = "CRITICAL" if ratio >= settings.vitastor_anomaly_relative_multiplier * 2 else "WARNING"
                    explanation = f"{key[0]} {key[1]}: {metric}={current:.2f}, baseline={baseline:.2f}, lệch {ratio:.2f}x ({len(values)} mẫu, baseline theo khung giờ khi đủ dữ liệu)"
                    if event is None:
                        event = VitastorAnomalyEvent(cluster_id=cluster_id, entity_type=key[0], entity_name=key[1], metric=metric, severity=severity, current_value=current, baseline_value=baseline, deviation_ratio=ratio, sample_count=len(values), explanation=explanation, detected_at=now, last_seen_at=now)
                        session.add(event); opened.append(explanation)
                    else:
                        event.current_value=current; event.baseline_value=baseline; event.deviation_ratio=ratio; event.sample_count=len(values); event.severity=severity; event.explanation=explanation; event.last_seen_at=now
                elif event is not None:
                    event.status="RESOLVED"; event.resolved_at=now; event.last_seen_at=now; resolved.append(event.explanation)
            session.add(VitastorEntityMetricSample(cluster_id=cluster_id, entity_type=entity["type"], entity_name=entity["name"], metrics_json=json.dumps(entity["metrics"]), collected_at=now))
        cutoff = now - timedelta(days=max(1, settings.vitastor_metric_retention_days))
        session.query(VitastorEntityMetricSample).filter(VitastorEntityMetricSample.collected_at < cutoff).delete()
        session.commit()
        current = session.query(VitastorAnomalyEvent).filter_by(cluster_id=cluster_id, status="OPEN").order_by(VitastorAnomalyEvent.detected_at.desc()).all()
        payload = [{"id": row.id, "entity_type": row.entity_type, "entity_name": row.entity_name, "metric": row.metric, "severity": row.severity, "current": row.current_value, "baseline": row.baseline_value, "ratio": row.deviation_ratio, "samples": row.sample_count, "explanation": row.explanation, "detected_at": row.detected_at.isoformat()+"Z"} for row in current]
    return {"open": payload, "opened": opened, "resolved": resolved}
