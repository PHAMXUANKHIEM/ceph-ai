import asyncio
import logging
import shlex
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from watcher import ceph_client
from watcher.ceph_client import CephQueryError

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()


def _normalize_pg_rows(payload: dict | list) -> list[dict]:
    """Normalize `ceph pg ls-by-pool --format json` across Ceph releases."""
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, dict):
        raw_rows = payload.get("pg_stats") or payload.get("pgs") or []
    else:
        raw_rows = []

    rows = []
    for pg in raw_rows:
        if not isinstance(pg, dict):
            continue
        stats = pg.get("stat_sum") if isinstance(pg.get("stat_sum"), dict) else {}
        rows.append(
            {
                "pgid": pg.get("pgid", "—"),
                "state": pg.get("state", "unknown"),
                "up": pg.get("up") if isinstance(pg.get("up"), list) else [],
                "acting": pg.get("acting") if isinstance(pg.get("acting"), list) else [],
                "objects": stats.get("num_objects", 0),
                "bytes": stats.get("num_bytes", 0),
                "degraded": stats.get("num_objects_degraded", 0),
                "misplaced": stats.get("num_objects_misplaced", 0),
                "unfound": stats.get("num_objects_unfound", 0),
                "last_scrub": pg.get("last_scrub_stamp") or "—",
            }
        )
    return sorted(rows, key=lambda row: str(row["pgid"]))


@router.get("/pgs", response_class=HTMLResponse)
async def pgs_page(request: Request, user: str = Depends(require_login)):
    pools = ceph_client.configured_rbd_pools()
    selected_pool = request.query_params.get("pool")
    if selected_pool and selected_pool not in pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    rows: list[dict] = []
    query_error: str | None = None
    if selected_pool:
        try:
            _host, payload = await asyncio.to_thread(
                ceph_client.run_ceph_json_command,
                f"ceph pg ls-by-pool {shlex.quote(selected_pool)}",
            )
            rows = _normalize_pg_rows(payload)
        except CephQueryError as exc:
            logger.warning("pgs_page: failed to query pool %r: %s", selected_pool, exc)
            query_error = str(exc)

    state_counts = Counter(row["state"] for row in rows)
    return templates.TemplateResponse(
        request,
        "pgs.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "pools": pools,
            "selected_pool": selected_pool,
            "pgs": rows,
            "state_counts": sorted(state_counts.items()),
            "query_error": query_error,
        },
    )
