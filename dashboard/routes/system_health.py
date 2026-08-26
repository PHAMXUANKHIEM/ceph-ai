from fastapi import APIRouter
from fastapi.responses import JSONResponse

from shared.service_health import status

router = APIRouter()


@router.get("/api/system/health")
def system_health():
    services = {name: status(name) for name in ("watcher", "worker")}
    healthy = all(value["healthy"] for value in services.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "services": services},
        status_code=200 if healthy else 503,
    )
