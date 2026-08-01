"""Bucket Access Log page (equivalent to Ceph's native S3 Bucket Logging
for older Ceph versions without it — see watcher/rgw_access_log.py's own
docstring for the full reasoning). Route-only reads, same AD-3 posture as
dashboard/routes/nodes.py's rgw_log_api — no S3 credentials involved
anywhere in this feature, only SSH access to an already-configured RGW
node.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from dashboard.vntime import to_utc_iso
from shared.cluster_nodes import configured_nodes as _configured_nodes
from watcher.rgw_access_log import RgwLogError, fetch_bucket_access_log

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


def _rgw_hosts() -> list[dict]:
    return [n for n in _configured_nodes() if "RGW" in n["roles"]]


@router.get("/bucket-access-log", response_class=HTMLResponse)
async def index(request: Request, user: str = Depends(require_login)):
    return templates.TemplateResponse(
        request,
        "bucket_access_log.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "rgw_hosts": _rgw_hosts(),
        },
    )


@router.get("/api/bucket-access-log")
async def bucket_access_log_api(host: str, bucket: str = "", user: str = Depends(require_login)):
    # Same SSRF-via-SSH whitelist posture as dashboard/routes/nodes.py's
    # rgw_log_api — `host` is attacker-reachable input, only an
    # already-configured RGW node may ever be queried.
    rgw_hosts = {n["host"] for n in _rgw_hosts()}
    if host not in rgw_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách RGW đã cấu hình")
    try:
        records = fetch_bucket_access_log(host, bucket)
    except RgwLogError as exc:
        logger.warning("bucket_access_log_api: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "host": host,
        "bucket": bucket,
        "records": [
            {
                "remote_addr": r["remote_addr"],
                "timestamp": to_utc_iso(r["timestamp"]) if r["timestamp"] else None,
                "timestamp_raw": r["timestamp_raw"],
                "method": r["method"],
                "path": r["path"],
                "bucket": r["bucket"],
                "object": r["object"],
                "action": r["action"],
                "status": r["status"],
                "bytes_sent": r["bytes_sent"],
            }
            for r in records
        ],
    }
