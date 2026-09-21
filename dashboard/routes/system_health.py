import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from shared.service_health import status
from shared import db
from shared.cluster_snapshot import read_snapshot
from shared.clusters import list_active_clusters
from shared.ceph_runner import get_metrics as get_ceph_runner_metrics
from shared.ceph_query_cache import get_metrics as get_ceph_cache_metrics
from watcher.cluster_snapshot_collector import get_metrics as get_collector_metrics
from shared.api_observability import get_metrics as get_api_metrics
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
                    "last_error": snapshot.get("last_error"),
                })
            return {"clusters": rows, "cluster_count": len(rows)}
    except Exception:
        logger.exception("system diagnostics: failed to read snapshot freshness")
        return {"clusters": [], "cluster_count": 0, "available": False}


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
    return {
        "metrics": get_ceph_runner_metrics(),
        "cache": get_ceph_cache_metrics(),
        "snapshot_collector": get_collector_metrics(),
        "api": get_api_metrics(),
        "retry": get_retry_metrics(),
        "natural_language": get_natural_language_metrics(),
        "websocket": get_websocket_metrics(),
        "snapshot_freshness": _snapshot_freshness_metrics(),
    }
