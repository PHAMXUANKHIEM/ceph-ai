from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from shared.service_health import status
from shared.ceph_runner import get_metrics as get_ceph_runner_metrics
from shared.ceph_query_cache import get_metrics as get_ceph_cache_metrics
from watcher.cluster_snapshot_collector import get_metrics as get_collector_metrics
from shared.api_observability import get_metrics as get_api_metrics
from shared.retry import get_metrics as get_retry_metrics

router = APIRouter()


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
    }
