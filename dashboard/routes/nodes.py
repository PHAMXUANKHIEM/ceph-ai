import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.cluster_nodes import configured_nodes as _configured_nodes
from watcher.node_metrics import NodeMetricsError, collect_node_metrics
from watcher.rgw_log import RgwLogError, fetch_rgw_log

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request, user: str = Depends(require_login)):
    nodes = _configured_nodes()
    requested_host = request.query_params.get("host")
    known_hosts = {n["host"] for n in nodes}
    if requested_host and requested_host not in known_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    # No default node — landing on /nodes with no ?host= shows the empty
    # "chọn một node" state, not whichever node happened to be configured
    # first. A node is only selected when the operator actually picks one
    # (or deep-links with ?host=).
    selected_host = requested_host
    selected_node = next((n for n in nodes if n["host"] == selected_host), None)
    return templates.TemplateResponse(
        request,
        "nodes.html",
        {"user": user, "nodes": nodes, "selected_host": selected_host, "selected_node": selected_node},
    )


@router.get("/api/nodes/{host}/metrics")
async def node_metrics_api(host: str, user: str = Depends(require_login)):
    # `host` is attacker-reachable input feeding straight into an SSH
    # connect() — without this whitelist, any logged-in user could make the
    # Dashboard open an SSH session to an arbitrary address of their
    # choosing using the Watcher keypair (SSRF-via-SSH). Only nodes the
    # operator already configured for this cluster are queryable.
    allowed_hosts = {n["host"] for n in _configured_nodes()}
    if host not in allowed_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    try:
        metrics = collect_node_metrics(host)
    except NodeMetricsError as exc:
        logger.warning("node_metrics_api: %s", exc)
        raise HTTPException(status_code=502, detail=f"Không lấy được metrics từ node: {exc}")
    return {"host": host, **metrics}


@router.get("/api/nodes/{host}/rgw-log")
async def rgw_log_api(host: str, filter: str = "", user: str = Depends(require_login)):
    """Backs the Nodes page's "Log RGW" panel — tails this host's radosgw
    daemon log (watcher/rgw_log.py), optionally grepped server-side by
    `filter`. Live-only, like node_metrics_api above: nothing here is
    persisted — this is a monitoring view, not a discrete auditable action.
    """
    # Restricted to hosts actually carrying the RGW role, not just any
    # configured node — same SSRF-via-SSH whitelist posture as
    # node_metrics_api, narrowed further here since a non-RGW host has no
    # radosgw container/daemon to read from anyway.
    rgw_hosts = {n["host"] for n in _configured_nodes() if "RGW" in n["roles"]}
    if host not in rgw_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách RGW đã cấu hình")
    try:
        output = fetch_rgw_log(host, filter)
    except RgwLogError as exc:
        logger.warning("rgw_log_api: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    lines = output.splitlines()
    return {"host": host, "filter": filter, "lines": lines}
