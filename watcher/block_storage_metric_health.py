"""Pure aggregation and alert-evidence helpers for Block Storage metrics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import quantiles
from typing import Iterable, Mapping

from shared import db
from shared.models import Incident, IncidentStatus, utc_now


def _number(row: Mapping[str, object], name: str) -> float:
    try:
        return max(0.0, float(row.get(name) or 0))
    except (TypeError, ValueError):
        return 0.0


def _when(row: Mapping[str, object]) -> datetime | None:
    value = row.get("polled_at") or row.get("captured_at")
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return float(quantiles(values, n=100, method="inclusive")[94])


def aggregate_pool_metrics(
    rows: Iterable[Mapping[str, object]],
    *,
    now: datetime,
    stale_after_seconds: int = 120,
    noisy_neighbor_ratio: float = 0.70,
) -> dict:
    """Aggregate volume samples by pool and identify stale/noisy evidence.

    Missing optional counters are represented as ``None`` rather than zero;
    zero means a real measured zero and must not be confused with an exporter
    that does not expose that metric.
    """
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        pool = str(row.get("pool") or "")
        if pool:
            grouped[pool].append(row)
    pools: list[dict] = []
    for pool, pool_rows in sorted(grouped.items()):
        by_image: dict[str, list[Mapping[str, object]]] = defaultdict(list)
        for row in pool_rows:
            by_image[str(row.get("image") or "unknown")].append(row)
        volumes: list[dict] = []
        for image, image_rows in by_image.items():
            latest = max((_when(row) for row in image_rows if _when(row)), default=None)
            iops = sum(_number(row, "iops") for row in image_rows)
            read_bps_values = [_number(row, "read_bytes_per_sec") for row in image_rows if row.get("read_bytes_per_sec") is not None]
            write_bps_values = [_number(row, "write_bytes_per_sec") for row in image_rows if row.get("write_bytes_per_sec") is not None]
            latency_values = [
                max(_number(row, "read_latency_ms"), _number(row, "write_latency_ms"))
                for row in image_rows
            ]
            p95_values = [_number(row, "p95_latency_ms") for row in image_rows if row.get("p95_latency_ms") is not None]
            volumes.append({
                "image": image,
                "iops": iops,
                "read_bytes_per_sec": sum(read_bps_values) if read_bps_values else None,
                "write_bytes_per_sec": sum(write_bps_values) if write_bps_values else None,
                "queue_depth": max((_number(row, "queue_depth") for row in image_rows if row.get("queue_depth") is not None), default=None),
                "latency_p95_ms": max(p95_values) if p95_values else _p95(latency_values),
                "latest_at": latest.isoformat() + "Z" if latest else None,
                "sample_count": len(image_rows),
            })
        volumes.sort(key=lambda item: (-item["iops"], item["image"]))
        total_iops = sum(item["iops"] for item in volumes)
        latest = max((_when(row) for row in pool_rows if _when(row)), default=None)
        freshness = None if latest is None else max(0.0, (now.replace(tzinfo=None) - latest).total_seconds())
        for item in volumes:
            item["share_of_pool_iops"] = item["iops"] / total_iops if total_iops else 0.0
        pools.append({
            "pool": pool,
            "sample_count": len(pool_rows),
            "volume_count": len(volumes),
            "iops": total_iops,
            "read_bytes_per_sec": sum(item["read_bytes_per_sec"] or 0 for item in volumes) or None,
            "write_bytes_per_sec": sum(item["write_bytes_per_sec"] or 0 for item in volumes) or None,
            "latency_p95_ms": _p95([item["latency_p95_ms"] for item in volumes if item["latency_p95_ms"] is not None]),
            "latest_at": latest.isoformat() + "Z" if latest else None,
            "freshness_seconds": freshness,
            "stale": latest is None or freshness > stale_after_seconds,
            "top_consumers": volumes[:10],
            "noisy_neighbors": [
                item for item in volumes
                if len(volumes) > 1 and item["share_of_pool_iops"] >= noisy_neighbor_ratio
            ],
        })
    return {
        "pools": pools,
        "stale_after_seconds": stale_after_seconds,
        "noisy_neighbor_ratio": noisy_neighbor_ratio,
        "read_only": True,
    }


def evaluate_metric_alerts(
    aggregates: Mapping[str, object],
    *,
    quota_state: Iterable[Mapping[str, object]] = (),
    jobs: Iterable[Mapping[str, object]] = (),
    now: datetime,
    stuck_after_seconds: int = 900,
) -> list[dict]:
    """Return stable alert descriptors; persistence/delivery stays elsewhere."""
    alerts: list[dict] = []
    for pool in aggregates.get("pools", []) or []:
        if pool.get("stale"):
            alerts.append({"kind": "BLOCK_STORAGE_METRIC_STALE", "pool": pool.get("pool"), "severity": "warning", "dedupe_key": f"metric-stale:{pool.get('pool')}"})
        if pool.get("near_full") or _number(pool, "used_percent") >= 85:
            alerts.append({"kind": "BLOCK_STORAGE_CAPACITY", "pool": pool.get("pool"), "severity": "critical" if _number(pool, "used_percent") >= 95 else "warning", "dedupe_key": f"capacity:{pool.get('pool')}"})
        for item in pool.get("noisy_neighbors", []) or []:
            alerts.append({"kind": "BLOCK_STORAGE_NOISY_NEIGHBOR", "pool": pool.get("pool"), "image": item.get("image"), "severity": "warning", "dedupe_key": f"noisy-neighbor:{pool.get('pool')}:{item.get('image')}"})
    for quota in quota_state:
        if quota.get("over_quota") or quota.get("near_quota"):
            alerts.append({"kind": "BLOCK_STORAGE_QUOTA", "pool": quota.get("pool"), "severity": "warning", "dedupe_key": f"quota:{quota.get('pool')}"})
    for job in jobs:
        started = _when({"polled_at": job.get("started_at")})
        if job.get("status") in {"RUNNING", "EXECUTING"} and started and (now.replace(tzinfo=None) - started).total_seconds() > stuck_after_seconds:
            alerts.append({"kind": "BLOCK_STORAGE_STUCK_JOB", "job_id": job.get("id"), "severity": "critical", "dedupe_key": f"stuck-job:{job.get('id')}"})
    return alerts


def sync_metric_alert_incidents(
    cluster_id: str | None,
    alerts: Iterable[Mapping[str, object]],
    *,
    evaluated_codes: set[str] | None = None,
) -> None:
    """Persist deduplicated metric alerts and resolve alerts absent this cycle."""
    if not cluster_id:
        return
    alerts = list(alerts)
    current = {(str(item.get("kind")), str(item.get("dedupe_key"))) for item in alerts}
    codes = evaluated_codes or ({code for code, _ in current} | {
        "BLOCK_STORAGE_METRIC_STALE", "BLOCK_STORAGE_NOISY_NEIGHBOR",
        "BLOCK_STORAGE_CAPACITY", "BLOCK_STORAGE_QUOTA", "BLOCK_STORAGE_STUCK_JOB",
    })
    with db.SessionLocal() as session:
        active_statuses = {
            IncidentStatus.NEW.value, IncidentStatus.DIAGNOSING.value,
            IncidentStatus.PENDING_APPROVAL.value, IncidentStatus.APPROVED.value,
            IncidentStatus.EXECUTING.value, IncidentStatus.GRACE_PENDING.value,
            IncidentStatus.VERIFYING.value,
        }
        active = session.query(Incident).filter(
            Incident.cluster_id == cluster_id,
            Incident.ceph_code.in_(codes),
            Incident.status.in_(active_statuses),
        ).all()
        active_by_key = {(row.ceph_code, row.dedupe_key): row for row in active}
        for row in active:
            if (row.ceph_code, row.dedupe_key) not in current:
                row.status = IncidentStatus.RESOLVED.value
        for item in alerts:
            key = (str(item.get("kind")), str(item.get("dedupe_key")))
            if key in active_by_key:
                continue
            session.add(Incident(
                cluster_id=cluster_id,
                ceph_code=key[0],
                dedupe_key=key[1],
                status=IncidentStatus.NEW.value,
                severity=str(item.get("severity") or "warning"),
                log_excerpt=f"Block Storage alert: {key[0]}",
                detected_at=utc_now(),
            ))
        session.commit()
