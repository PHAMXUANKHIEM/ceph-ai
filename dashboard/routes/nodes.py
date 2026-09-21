import logging
from datetime import datetime, timedelta
from shared.time import utc_now

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.cluster_scope import cluster_selection, selected_cluster
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared.cluster_nodes import configured_nodes as _configured_nodes
from shared.cluster_nodes import resolve_ssh_creds
from shared.cluster_snapshot import DEFAULT_MAX_STALE_SECONDS, read_section_snapshot
from shared.telemetry_nodes import collapse_nodes
from shared import db
from shared.models import HostMetricSample
from shared.object_storage_cache import get_or_load, state as cache_state
from watcher.node_metrics import NodeMetricsError, collect_node_metrics, collect_node_metrics_with
from watcher.ceph_log import CephLogError, fetch_ceph_log, fetch_ceph_log_with
from watcher.rgw_log import RgwLogError, fetch_rgw_log, fetch_rgw_log_with

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()
INVENTORY_STALE_SECONDS = 90

# The browser sends the short value because it is stable in URLs and easy to
# read.  Keep the numeric aliases as well so old bookmarked/API URLs continue
# to work while the selector is migrated.
NODE_TIME_RANGES = {
    "2m": 120,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
    "14d": 1209600,
}
NODE_TIME_RANGE_ALIASES = {str(seconds): name for name, seconds in NODE_TIME_RANGES.items()}
NODE_RANGE_MAX_POINTS = {"2m": 24, "5m": 30, "15m": 30, "1h": 40, "24h": 144, "7d": 168, "14d": 240}


def _normalize_node_range(value: str | None) -> str:
    value = (value or "5m").strip().lower()
    return value if value in NODE_TIME_RANGES else NODE_TIME_RANGE_ALIASES.get(value, "5m")


def _metric_dict(row: HostMetricSample, at: datetime | None = None) -> dict:
    return {
        "at": (at or row.collected_at).isoformat() + "Z",
        "cpu_percent": row.cpu_percent,
        "mem_percent": row.mem_percent,
        "disk_read_iops": row.disk_read_iops,
        "disk_write_iops": row.disk_write_iops,
        "disk_latency_ms": row.disk_latency_ms,
    }


def _live_metric_dict(metrics: dict, at: datetime) -> dict:
    return {
        "at": at.isoformat() + "Z",
        **{key: metrics.get(key) for key in (
            "cpu_percent", "mem_percent", "disk_read_iops",
            "disk_write_iops", "disk_latency_ms",
        )},
    }


def _average_metrics(points: list[dict]) -> dict:
    fields = ("cpu_percent", "mem_percent", "disk_read_iops", "disk_write_iops", "disk_latency_ms")
    result = {}
    for field in fields:
        values = [point[field] for point in points if isinstance(point.get(field), (int, float))]
        result[field] = round(sum(values) / len(values), 2) if values else None
    return result


def _downsample_metrics(points: list[dict], max_points: int) -> list[dict]:
    """Reduce chart payload size while preserving the mean of each bucket."""
    if len(points) <= max_points:
        return points
    bucket_size = (len(points) + max_points - 1) // max_points
    fields = ("cpu_percent", "mem_percent", "disk_read_iops", "disk_write_iops", "disk_latency_ms")
    sampled = []
    for start in range(0, len(points), bucket_size):
        bucket = points[start:start + bucket_size]
        representative = {"at": bucket[len(bucket) // 2]["at"]}
        for field in fields:
            values = [point[field] for point in bucket if isinstance(point.get(field), (int, float))]
            representative[field] = round(sum(values) / len(values), 2) if values else None
        sampled.append(representative)
    return sampled


def _nodes_for_cluster(cluster):
    # The default row mirrors `.env` only at lifecycle sync points; use the
    # live Settings singleton for it, exactly as legacy callers did.
    configured = _configured_nodes() if cluster.is_default else _configured_nodes(cluster)
    identities = {}
    if configured and cluster.id:
        from sqlalchemy import func

        with db.SessionLocal() as session:
            latest = (
                session.query(
                    HostMetricSample.host,
                    HostMetricSample.node_name,
                    func.max(HostMetricSample.collected_at).label("latest_at"),
                )
                .filter(HostMetricSample.cluster_id == cluster.id)
                .group_by(HostMetricSample.host, HostMetricSample.node_name)
                .order_by(func.max(HostMetricSample.collected_at).desc())
                .all()
            )
        for host, node_name, _latest_at in latest:
            if host and node_name:
                identities.setdefault(host, node_name)
    return collapse_nodes(configured, identities)


def _collapse_snapshot_nodes(cluster, snapshot_nodes):
    """Normalize old six-address snapshots before they reach the browser."""
    display_nodes = _nodes_for_cluster(cluster)
    identities = {}
    for node in display_nodes:
        node_name = node.get("node_name")
        if not node_name:
            continue
        for alias in [node.get("host"), *(node.get("aliases") or [])]:
            if alias:
                identities[alias] = node_name
    return collapse_nodes(snapshot_nodes, identities)


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    selected_range = _normalize_node_range(request.query_params.get("range"))
    snapshot = read_section_snapshot(
        cluster.id,
        "nodes",
        stale_after_seconds=INVENTORY_STALE_SECONDS,
        max_stale_seconds=DEFAULT_MAX_STALE_SECONDS,
    )
    snapshot_data = snapshot.get("nodes", {}) if snapshot else {}
    snapshot_nodes = snapshot_data.get("nodes") if isinstance(snapshot_data, dict) else None
    # Configuration is a safe local fallback until the first Watcher inventory
    # snapshot exists; it does not open an SSH session.
    nodes = (
        _collapse_snapshot_nodes(cluster, snapshot_nodes)
        if isinstance(snapshot_nodes, list)
        else _nodes_for_cluster(cluster)
    )
    requested_host = request.query_params.get("host")
    # Keep old alias URLs valid even though the selector now displays one
    # physical node per hostname.
    known_hosts = {n["host"] for n in _configured_nodes() if n.get("host")} if cluster.is_default else {
        n["host"] for n in _configured_nodes(cluster) if n.get("host")
    }
    if requested_host and requested_host not in known_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    # No default node — landing on /nodes with no ?host= shows the empty
    # "chọn một node" state, not whichever node happened to be configured
    # first. A node is only selected when the operator actually picks one
    # (or deep-links with ?host=).
    selected_host = requested_host
    selected_node = next(
        (
            n for n in nodes
            if n["host"] == selected_host or selected_host in n.get("aliases", [])
        ),
        None,
    )
    if selected_node is not None:
        selected_host = selected_node["host"]

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
            "selected_range": selected_range,
            "snapshot_meta": {
                "collected_at": snapshot.get("collected_at") if snapshot else None,
                "age_seconds": snapshot.get("age_seconds") if snapshot else None,
                "stale": bool(snapshot.get("stale", True)) if snapshot else True,
                "last_error": snapshot.get("last_error") if snapshot else None,
            },
        },
    )


@router.get("/api/nodes/summary")
async def node_summary_api(request: Request, user: str = Depends(require_login)):
    """Return the persisted cluster node inventory without opening SSH."""
    cluster = selected_cluster(request)
    snapshot = read_section_snapshot(
        cluster.id,
        "nodes",
        stale_after_seconds=INVENTORY_STALE_SECONDS,
        max_stale_seconds=DEFAULT_MAX_STALE_SECONDS,
    )
    data = snapshot.get("nodes", {}) if snapshot else {}
    if not isinstance(data, dict):
        data = {}
    elif isinstance(data.get("nodes"), list):
        data = dict(data)
        data["nodes"] = _collapse_snapshot_nodes(cluster, data["nodes"])
        data["total"] = len(data["nodes"])
    return {
        "data": data,
        "meta": {
            "cluster_id": cluster.id,
            "generation": snapshot.get("generation", 0) if snapshot else 0,
            "collected_at": snapshot.get("collected_at") if snapshot else None,
            "published_at": snapshot.get("published_at") if snapshot else None,
            "age_seconds": snapshot.get("age_seconds") if snapshot else None,
            "stale": bool(snapshot.get("stale", True)) if snapshot else True,
            "available": bool(snapshot and snapshot.get("section_available", True)),
            "last_error": snapshot.get("last_error") if snapshot else None,
        },
    }


@router.get("/api/nodes/{host}/metrics")
async def node_metrics_api(request: Request, host: str, user: str = Depends(require_login)):
    # `host` is attacker-reachable input feeding straight into an SSH
    # connect() — without this whitelist, any logged-in user could make the
    # Dashboard open an SSH session to an arbitrary address of their
    # choosing using the Watcher keypair (SSRF-via-SSH). Only nodes the
    # operator already configured for this cluster are queryable.
    cluster = selected_cluster(request)
    configured = _configured_nodes() if cluster.is_default else _configured_nodes(cluster)
    allowed_hosts = {n["host"] for n in configured if n.get("host")}
    display_nodes = _nodes_for_cluster(cluster)
    canonical = next(
        (node["host"] for node in display_nodes if host == node["host"] or host in node.get("aliases", [])),
        host,
    )
    if host not in allowed_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    host = canonical
    range_name = _normalize_node_range(
        request.query_params.get("range")
        or request.query_params.get("time_range")
        or request.query_params.get("duration")
    )
    range_seconds = NODE_TIME_RANGES[range_name]
    now = utc_now()

    # Read the persisted window first. It is local database data and should
    # be available immediately, even when a node's SSH endpoint is slow.
    cutoff = now - timedelta(seconds=range_seconds)
    with db.SessionLocal() as session:
        history_rows = session.query(HostMetricSample).filter(
            HostMetricSample.cluster_id == cluster.id,
            HostMetricSample.host == host,
            HostMetricSample.collected_at >= cutoff,
            HostMetricSample.collected_at <= now,
        ).order_by(HostMetricSample.collected_at.asc()).all()

    metrics_error = None
    try:
        def load_metrics():
            if cluster.is_default:
                return collect_node_metrics(host)
            ssh_user, ssh_key_path, _mode, _container = resolve_ssh_creds(cluster)
            return collect_node_metrics_with(host, ssh_user, ssh_key_path)
        metrics = get_or_load(
            "node-metrics",
            f"{cluster.id}:{host}",
            load_metrics,
            # The browser polls this endpoint every 3 seconds. A five-minute
            # TTL makes the chart repeat one sample for almost the whole
            # visible window, so keep only a short deduplication window and
            # refresh stale data in the background when possible.
            ttl_seconds=2,
            stale_ttl_seconds=10,
            # Never make the chart wait for a first SSH sample. The Watcher
            # history above is enough to paint the chart immediately; this
            # refresh fills the live cache for the next poll.
            background_on_miss=True,
            fallback=None,
        )
        # ``background_on_miss`` deliberately hides loader exceptions from
        # the request thread. Inspect the cache state so a completed failed
        # refresh is reported as a stale/error state instead of remaining
        # stuck at ``live_pending`` forever.
        live_cache_state = cache_state("node-metrics", f"{cluster.id}:{host}")
        if live_cache_state["error"] and not live_cache_state["refreshing"]:
            metrics_error = "Không thể lấy telemetry realtime từ node"
    except NodeMetricsError as exc:
        logger.warning("node_metrics_api: %s", exc)
        metrics = None
        metrics_error = str(exc)

    # HostMetricSample is already collected by Watcher for RCA, so use it as
    # the historical source and add a live point only when the cache already
    # has one. This keeps first paint fast and preserves realtime updates on
    # the following poll.
    history_points = [_metric_dict(row) for row in history_rows]
    live_pending = metrics is None and metrics_error is None
    if metrics is None:
        # A first request schedules live collection in the background. Keep
        # the chart available immediately from history; if history is empty,
        # return an explicit no-data state and let the next poll pick up the
        # freshly cached live sample.
        if history_rows:
            latest_row = history_rows[-1]
            metrics = {
                field: getattr(latest_row, field)
                for field in (
                    "cpu_percent", "mem_percent", "disk_read_iops",
                    "disk_write_iops", "disk_latency_ms",
                )
            }
            current_point = history_points[-1]
            all_points = history_points
            chart_points = _downsample_metrics(history_points, NODE_RANGE_MAX_POINTS[range_name])
        else:
            metrics = {
                field: None
                for field in (
                    "cpu_percent", "mem_percent", "disk_read_iops",
                    "disk_write_iops", "disk_latency_ms",
                )
            }
            current_point = {"at": now.isoformat() + "Z", **metrics}
            all_points = []
            chart_points = []
    else:
        current_point = _live_metric_dict(metrics, now)
        all_points = history_points + [current_point]
        # A live sample can have the same second as a persisted sample. Keep
        # the live value as the newest point, but avoid duplicate timestamps.
        if len(all_points) > 1 and all_points[-2]["at"] == all_points[-1]["at"]:
            all_points[-2] = all_points[-1]
            all_points.pop()
        # Keep the fresh point at the right edge; if it were included in the
        # last averaging bucket, the chart would lose the actual current value.
        chart_points = _downsample_metrics(
            history_points, max(NODE_RANGE_MAX_POINTS[range_name] - 1, 1)
        ) + [current_point]

    return {
        "host": host,
        # Keep the legacy top-level fields for API consumers outside the
        # Nodes page; the chart-aware fields below are additive.
        **metrics,
        "range": range_name,
        "range_seconds": range_seconds,
        "range_start": cutoff.isoformat() + "Z",
        "range_end": now.isoformat() + "Z",
        "sample_count": len(all_points),
        "source_sample_count": len(history_points),
        "live_available": metrics_error is None and not live_pending,
        "live_pending": live_pending,
        "live_error": metrics_error,
        "points": chart_points,
        "current": current_point,
        "summary": _average_metrics(all_points),
        "summary_mode": "average",
    }


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
