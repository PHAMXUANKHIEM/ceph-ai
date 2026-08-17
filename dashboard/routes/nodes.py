import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.cluster_scope import cluster_selection, selected_cluster
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.cluster_nodes import configured_nodes as _configured_nodes
from shared.cluster_nodes import resolve_ssh_creds
from shared.object_storage_cache import get_or_load
from watcher.node_metrics import NodeMetricsError, collect_node_metrics, collect_node_metrics_with
from watcher.ceph_log import CephLogError, fetch_ceph_log, fetch_ceph_log_with
from watcher.rgw_log import RgwLogError, fetch_rgw_log, fetch_rgw_log_with

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


def _nodes_for_cluster(cluster):
    # The default row mirrors `.env` only at lifecycle sync points; use the
    # live Settings singleton for it, exactly as legacy callers did.
    return _configured_nodes() if cluster.is_default else _configured_nodes(cluster)


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    nodes = _nodes_for_cluster(cluster)
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
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "nodes": nodes,
            "selected_host": selected_host,
            "selected_node": selected_node,
            "clusters": clusters,
            "selected_cluster": cluster,
        },
    )


@router.get("/api/nodes/{host}/metrics")
async def node_metrics_api(request: Request, host: str, user: str = Depends(require_login)):
    # `host` is attacker-reachable input feeding straight into an SSH
    # connect() — without this whitelist, any logged-in user could make the
    # Dashboard open an SSH session to an arbitrary address of their
    # choosing using the Watcher keypair (SSRF-via-SSH). Only nodes the
    # operator already configured for this cluster are queryable.
    cluster = selected_cluster(request)
    allowed_hosts = {n["host"] for n in _nodes_for_cluster(cluster)}
    if host not in allowed_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    try:
        def load_metrics():
            if cluster.is_default:
                return collect_node_metrics(host)
            ssh_user, ssh_key_path, _mode, _container = resolve_ssh_creds(cluster)
            return collect_node_metrics_with(host, ssh_user, ssh_key_path)
        metrics = get_or_load("node-metrics", f"{cluster.id}:{host}", load_metrics, ttl_seconds=300)
    except NodeMetricsError as exc:
        logger.warning("node_metrics_api: %s", exc)
        raise HTTPException(status_code=502, detail=f"Không lấy được metrics từ node: {exc}")
    return {"host": host, **metrics}


@router.get("/api/nodes/{host}/rgw-log")
async def rgw_log_api(request: Request, host: str, filter: str = "", user: str = Depends(require_login)):
    """Backs the Nodes page's "Log RGW" panel — tails this host's radosgw
    daemon log (watcher/rgw_log.py), optionally grepped server-side by
    `filter`. Live-only, like node_metrics_api above: nothing here is
    persisted — this is a monitoring view, not a discrete auditable action.
    """
    # Restricted to hosts actually carrying the RGW role, not just any
    # configured node — same SSRF-via-SSH whitelist posture as
    # node_metrics_api, narrowed further here since a non-RGW host has no
    # radosgw container/daemon to read from anyway.
    cluster = selected_cluster(request)
    rgw_hosts = {n["host"] for n in _nodes_for_cluster(cluster) if "RGW" in n["roles"]}
    if host not in rgw_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách RGW đã cấu hình")
    try:
        if cluster.is_default:
            output = fetch_rgw_log(host, filter)
        else:
            ssh_user, ssh_key_path, exec_mode, _container = resolve_ssh_creds(cluster)
            output = fetch_rgw_log_with(
                host, filter, ssh_user, ssh_key_path, exec_mode, cluster.ceph_rgw_container_name
            )
    except RgwLogError as exc:
        logger.warning("rgw_log_api: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    lines = output.splitlines()
    return {"host": host, "filter": filter, "lines": lines}


@router.get("/api/nodes/{host}/ceph-log")
async def ceph_log_api(
    request: Request, host: str, service: str, filter: str = "",
    user: str = Depends(require_login),
):
    """Read a bounded daemon-log tail from a configured node and cluster."""
    cluster = selected_cluster(request)
    node = next((n for n in _nodes_for_cluster(cluster) if n["host"] == host), None)
    requested_role = service.strip().upper()
    if node is None or requested_role not in node["roles"]:
        raise HTTPException(
            status_code=404,
            detail="Node không có dịch vụ Ceph được yêu cầu trong cấu hình cụm",
        )
    try:
        if cluster.is_default:
            output = fetch_ceph_log(host, service.strip().lower(), filter)
        else:
            ssh_user, ssh_key_path, exec_mode, mon_container = resolve_ssh_creds(cluster)
            output = fetch_ceph_log_with(
                host, service.strip().lower(), filter, ssh_user, ssh_key_path,
                exec_mode, mon_container, cluster.ceph_osd_container_name,
                cluster.ceph_rgw_container_name,
            )
    except CephLogError as exc:
        logger.warning("ceph_log_api: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return {"host": host, "service": service.strip().lower(), "filter": filter,
            "lines": output.splitlines()}
