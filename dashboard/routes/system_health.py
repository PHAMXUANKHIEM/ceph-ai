from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from shared.service_health import status
from shared.ceph_runner import get_metrics as get_ceph_runner_metrics

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
    return {"metrics": get_ceph_runner_metrics()}
