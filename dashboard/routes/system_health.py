import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from shared.service_health import status
from shared import db
from shared.cluster_snapshot import read_snapshot
from shared.clusters import list_active_clusters
from shared.cluster_events import get_metrics as get_event_metrics
from shared.ceph_runner import get_metrics as get_ceph_runner_metrics
from watcher.ceph_client import get_cephadm_circuit_metrics, get_mon_circuit_metrics
from shared.ceph_query_cache import get_metrics as get_ceph_cache_metrics
from shared.ceph_query_cache import get_storage_metrics
from watcher.cluster_snapshot_collector import get_metrics as get_collector_metrics
from shared.api_observability import get_metrics as get_api_metrics
from shared.realtime_controls import circuit_metrics, get_metrics as get_realtime_control_metrics
from shared.realtime_observability import get_metrics as get_realtime_observability_metrics
from shared.retry import get_metrics as get_retry_metrics
from shared.natural_language.nl_metrics import get_natural_language_metrics
from dashboard.ws import get_metrics as get_websocket_metrics

router = APIRouter()
logger = logging.getLogger(__name__)


def _snapshot_freshness_metrics() -> dict[str, object]:
    """Return bounded per-cluster freshness data for admin diagnostics."""
    try:
        with db.SessionLocal() as session:
            clusters = list_active_clusters(session)
            rows = []
            for cluster in clusters:
                snapshot = read_snapshot(cluster.id)
                if snapshot is None:
                    rows.append({"cluster_id": cluster.id, "available": False})
                    continue
                rows.append({
                    "cluster_id": cluster.id,
                    "available": bool(snapshot.get("available", True)),
                    "generation": snapshot.get("generation"),
                    "collected_at": snapshot.get("collected_at"),
                    "last_attempted_at": snapshot.get("last_attempted_at"),
                    "age_seconds": snapshot.get("age_seconds"),
                    "stale": bool(snapshot.get("stale")),
                    "refreshing": bool(snapshot.get("refreshing")),
                    "collector_lag_seconds": snapshot.get("collector_lag_seconds"),
                    "last_error": snapshot.get("last_error"),
                })
            return {"clusters": rows, "cluster_count": len(rows)}
    except Exception:
        logger.exception("system diagnostics: failed to read snapshot freshness")
        return {"clusters": [], "cluster_count": 0, "available": False}


def _operational_alerts(
    freshness: dict[str, object],
    services: dict[str, dict],
    event_bus: dict[str, object],
    cache_storage: dict[str, object],
) -> list[dict[str, object]]:
    """Build bounded operator alerts from the realtime data-path signals."""
    alerts: list[dict[str, object]] = []
    for row in freshness.get("clusters", []):
        if not isinstance(row, dict):
            continue
        cluster_id = row.get("cluster_id")
        age = row.get("age_seconds")
        if not row.get("available"):
            alerts.append({
                "code": "snapshot_unavailable",
                "severity": "critical",
                "cluster_id": cluster_id,
                "message": "Chưa có snapshot realtime cho cluster.",
            })
        elif row.get("stale"):
            alerts.append({
                "code": "snapshot_stale",
                "severity": "warning",
                "cluster_id": cluster_id,
                "age_seconds": age,
                "message": "Snapshot đã quá hạn freshness; kiểm tra Watcher hoặc MON.",
            })
        if row.get("last_error"):
            alerts.append({
                "code": "snapshot_refresh_error",
                "severity": "warning",
                "cluster_id": cluster_id,
                "message": str(row["last_error"])[:240],
            })

    watcher = services.get("watcher") or {}
    if not watcher.get("healthy"):
        alerts.append({
            "code": "watcher_heartbeat_stale",
            "severity": "critical",
            "message": "Watcher không còn heartbeat hợp lệ.",
        })
    recent_failures = int(event_bus.get("publish_failure_window_15m", 0) or 0)
    last_failure = float(event_bus.get("last_failure_at") or 0)
    last_success = float(event_bus.get("last_success_at") or 0)
    if recent_failures > 0 and last_failure > last_success:
        alerts.append({
            "code": "event_bus_publish_failed",
            "severity": "warning",
            "message": "Event bus có lỗi publish trong 15 phút gần đây; realtime có thể chậm.",
            "failure_total": int(event_bus.get("publish_failure_total", 0) or 0),
            "failure_window_15m": recent_failures,
            "last_failure_at": last_failure,
            "last_success_at": last_success or None,
            "recovery_state": str(event_bus.get("publish_recovery_state") or "ACTIVE"),
        })
    if cache_storage.get("available") and int(cache_storage.get("bytes", 0)) > settings.ceph_snapshot_cache_max_bytes:
        alerts.append({
            "code": "snapshot_cache_growth",
            "severity": "warning",
            "bytes": int(cache_storage["bytes"]),
            "limit_bytes": settings.ceph_snapshot_cache_max_bytes,
            "message": "Disk cache snapshot đã vượt ngưỡng cấu hình.",
        })
    return alerts[:50]


@router.get("/api/system/health")
def system_health():
    services = {name: status(name) for name in ("watcher", "worker")}
    healthy = all(value["healthy"] for value in services.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "services": services},
        status_code=200 if healthy else 503,
    )


@router.get("/api/debug/ceph-latency")
def ceph_latency_debug(user: str = Depends(require_login)):
    """Admin-only bounded SSH/command diagnostics; never exposes secrets."""
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được xem chẩn đoán Ceph")
    # Keep ``metrics`` backward-compatible for existing admin tooling while
    # exposing the cache and collector counters as additive fields.
    freshness = _snapshot_freshness_metrics()
    services = {name: status(name) for name in ("watcher", "worker")}
    event_bus = get_event_metrics()
    cache_storage = get_storage_metrics()
    return {
        "metrics": get_ceph_runner_metrics(),
        "mon_circuit": get_mon_circuit_metrics(),
        "cephadm_circuit": get_cephadm_circuit_metrics(),
        "cache": get_ceph_cache_metrics(),
        "snapshot_collector": get_collector_metrics(),
        "api": get_api_metrics(),
        "retry": get_retry_metrics(),
        "natural_language": get_natural_language_metrics(),
        "websocket": get_websocket_metrics(),
        "snapshot_freshness": freshness,
        "realtime": get_realtime_observability_metrics(),
        "realtime_controls": get_realtime_control_metrics(),
        "realtime_circuits": circuit_metrics(),
        "event_bus": event_bus,
        "cache_storage": cache_storage,
        "operational_alerts": _operational_alerts(freshness, services, event_bus, cache_storage),
    }
