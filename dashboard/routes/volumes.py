import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.incidents import OPEN_STATUSES
from dashboard.templating import make_templates
from shared import db
from shared.models import Incident
from watcher import ceph_client
from watcher.ceph_client import CephQueryError
from watcher.volume_monitor import ceph_code_for

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


@router.get("/volumes", response_class=HTMLResponse)
async def volumes_page(request: Request, user: str = Depends(require_login)):
    pools = ceph_client.configured_rbd_pools()
    requested_pool = request.query_params.get("pool")
    if requested_pool and requested_pool not in pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    # No default pool — same "must actually pick one" posture as
    # dashboard/routes/nodes.py's selected_host (landing on /volumes with
    # no ?pool= shows the empty "chọn một pool" state).
    selected_pool = requested_pool
    return templates.TemplateResponse(
        request,
        "volumes.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "pools": pools,
            "selected_pool": selected_pool,
        },
    )


@router.get("/api/volumes/{pool}/iostat")
async def volume_iostat_api(pool: str, user: str = Depends(require_login)):
    # `pool` is attacker-reachable input feeding into an `rbd` command run
    # over SSH — same SSRF-via-SSH whitelist posture as
    # dashboard/routes/nodes.py::node_metrics_api's `host` check. Only pools
    # the operator already configured (settings.ceph_rbd_pools) are queryable.
    allowed_pools = set(ceph_client.configured_rbd_pools())
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    try:
        samples = ceph_client.query_rbd_iostat(pool)
    except CephQueryError as exc:
        logger.warning("volume_iostat_api: %s", exc)
        raise HTTPException(status_code=502, detail=f"Không lấy được iostat từ cụm: {exc}")

    # Cross-reference each returned image against any currently-OPEN
    # VOLUME_SATURATED: Incident (watcher/volume_monitor.py owns creating/
    # resolving these) — one query for the whole pool rather than one
    # per-image DB round trip.
    with db.SessionLocal() as session:
        open_codes = {
            incident.ceph_code
            for incident in session.query(Incident)
            .filter(Incident.ceph_code.like("VOLUME_SATURATED:%"))
            .filter(Incident.status.in_(OPEN_STATUSES))
            .all()
        }

    images = [
        {
            "image": sample["image"],
            "iops": sample["iops"],
            "read_latency_ms": sample["read_latency_ms"],
            "write_latency_ms": sample["write_latency_ms"],
            "saturated": ceph_code_for(pool, sample["image"]) in open_codes,
        }
        for sample in samples
    ]
    return {"pool": pool, "images": images}
