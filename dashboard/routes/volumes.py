import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import logging
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import or_

from config.settings import settings
from dashboard import volume_perf_analysis
from dashboard.cinder_discovery import (
    discover_cinder_snapshots,
    discover_cinder_volume,
    reconcile_cinder_attachment,
)
from dashboard.cluster_scope import cluster_connection, cluster_selection, selected_cluster
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.incidents import OPEN_STATUSES, _resolve_selected_cluster
from dashboard.templating import make_templates
from dashboard.vntime import format_vn_clock
from shared import audit, db
from shared.cluster_nodes import resolve_ssh_creds
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    BackupJob,
    Incident,
    IncidentStatus,
    VolumeMetric,
    VolumePerfSweep,
)
from watcher import ceph_client
from watcher.ceph_client import CephQueryError, run_ceph_json_command_with
from watcher.volume_monitor import ceph_code_for
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)

# 2026-07-29: bounds for /api/volumes/{pool}/{image}/history's own `hours`
# query param — VolumeMetric is this app's fastest-growing table (see that
# model's own docstring: one row per pool x image EVERY poll, no automatic
# pruning), so an unbounded "give me everything" window is a real footgun
# for an operator with a long-lived, never-purged install. The PEAK value
# itself is still computed over the table's ENTIRE retained history
# regardless of this window (see volume_history_api below) — only the
# plotted time-series is bounded.
_DEFAULT_HISTORY_HOURS = 6
_MAX_HISTORY_HOURS = 168


def _pool_names_from_detail(payload: dict | list) -> list[str]:
    rows = payload if isinstance(payload, list) else payload.get("pools") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    names = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("pool_name") or row.get("poolname")
        applications = row.get("application_metadata")
        if name and isinstance(applications, dict) and "rbd" in applications:
            names.append(str(name))
    return sorted(set(names))


def _rbd_pools_for_request(request: Request) -> list[str]:
    """Return RBD pools for the cluster selected in this browser session."""
    _clusters, cluster = _resolve_selected_cluster(
        request.query_params.get("cluster", "").strip(),
        request.session.get("selected_cluster_id", ""),
    )
    mon_nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)
    try:
        _host, payload = run_ceph_json_command_with(
            mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode,
            "ceph osd pool ls detail",
        )
        return _pool_names_from_detail(payload)
    except CephQueryError as exc:
        logger.warning("_rbd_pools_for_request: cluster %s discovery failed: %s", cluster.id, exc)
        # The live cluster is authoritative. The configured list is only a
        # continuity fallback for the default cluster during an SSH outage.
        return ceph_client.configured_rbd_pools() if cluster.is_default else []


def _cluster_for_request(request: Request):
    cluster = selected_cluster(request)
    requested = request.query_params.get("cluster", "").strip()
    if requested and cluster.id != requested:
        raise HTTPException(
            status_code=409,
            detail="Cluster được yêu cầu không tồn tại hoặc đã bị vô hiệu hóa; không fallback sang cluster mặc định",
        )
    return cluster


def _allowed_pools_for_request(request: Request) -> tuple[object, set[str]]:
    cluster = _cluster_for_request(request)
    return cluster, set(_rbd_pools_for_request(request))


def _cluster_row_filter(column, cluster):
    """Legacy NULL rows belong to the default cluster."""
    return or_(column == cluster.id, column.is_(None)) if cluster.is_default else column == cluster.id


def _require_default_cluster_operation(request: Request):
    cluster = _cluster_for_request(request)
    if not cluster.is_default:
        raise HTTPException(status_code=409, detail="Thao tác ghi trên Pool hiện chỉ hỗ trợ cluster mặc định")
    return cluster

router = APIRouter()
templates = make_templates()

# Synthetic Incident.ceph_code for this feature — same trick
# dashboard/routes/deploy_cluster.py/delete_cluster.py/upgrade.py/patch.py
# use: AuditEntry.incident_id is a required FK, and purging an already-
# trashed RBD image has no real detected Incident behind it, only an
# operator explicitly clicking "Xoá" on the Volumes page.
RBD_TRASH_REMOVE_CEPH_CODE = "RBD_TRASH_REMOVE"
RBD_TRASH_REMOVE_ACTION_ID = "rbd_trash_remove"
RBD_TRASH_PURGE_ALL_ACTION_ID = "rbd_trash_purge_all"
# Distinct synthetic incident for the approval-gated bulk purge. Keeping its
# code/action family separate from per-image removal makes dedup and audit
# evidence unambiguous and records the exact trash-ID snapshot being approved.
RBD_TRASH_PURGE_ALL_CEPH_CODE = "RBD_TRASH_PURGE_ALL"
RBD_VOLUME_CREATE_CEPH_CODE = "RBD_VOLUME_CREATE"
RBD_VOLUME_RESIZE_CEPH_CODE = "RBD_VOLUME_RESIZE"
RBD_VOLUME_RENAME_CEPH_CODE = "RBD_VOLUME_RENAME"
RBD_VOLUME_TRASH_MOVE_CEPH_CODE = "RBD_VOLUME_TRASH_MOVE"
RBD_VOLUME_TRASH_RESTORE_CEPH_CODE = "RBD_VOLUME_TRASH_RESTORE"
CINDER_VOLUME_ATTACH_CEPH_CODE = "CINDER_VOLUME_ATTACH"
CINDER_VOLUME_DETACH_CEPH_CODE = "CINDER_VOLUME_DETACH"
CINDER_SNAPSHOT_CREATE_CEPH_CODE = "CINDER_SNAPSHOT_CREATE"

# 2026-07-29: "Đo hiệu năng tối đa" (load sweep) button — same synthetic-
# incident trick as RBD_TRASH_REMOVE_CEPH_CODE above (an operator-initiated
# action with no detected Incident behind it). Executed by
# worker/executor/volume_perf.py, dispatched via worker/policy/gate.py's
# own `volume_perf_action_ids` family (see that yaml's own comment).
VOLUME_PERF_SWEEP_CEPH_CODE = "VOLUME_PERF_SWEEP"
VOLUME_PERF_SWEEP_ACTION_ID = "volume_perf_sweep"
VM_PERF_BENCHMARK_CEPH_CODE = "VM_PERF_BENCHMARK"
VM_PERF_BENCHMARK_ACTION_ID = "vm_perf_benchmark"
_SSH_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_VM_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9._+-]+$")
_RBD_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_OPENSTACK_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_CINDER_SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)
_RBD_VOLUME_MUTATION_ACTION_IDS = (
    "rbd_create_volume", "rbd_resize_volume", "rbd_rename_volume",
    "rbd_trash_move_volume", "rbd_trash_restore_volume",
    "cinder_attach_volume", "cinder_detach_volume",
    "cinder_create_snapshot",
)


# 2026-07-28: same "own copy, not a cross-import" posture as
# dashboard/routes/{settings,users,maintenance}.py's identical helper.
def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )


def _in_flight_trash_actions(pool: str) -> dict[str, Action]:
    """{trash_id: Action} for every rbd_trash_remove Action still
    PENDING_APPROVAL/APPROVED for this pool — action_params is a JSON TEXT
    column (no portable cross-DB JSON-field query in this codebase, same
    posture as everywhere else Action.action_params gets filtered), so this
    loads the (small) set of in-flight trash-removal Actions and matches in
    Python rather than in SQL."""
    result: dict[str, Action] = {}
    with db.SessionLocal() as session:
        rows = (
            session.query(Action)
            .filter(Action.action_id == RBD_TRASH_REMOVE_ACTION_ID)
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .all()
        )
        for action in rows:
            try:
                params = json.loads(action.action_params) if action.action_params else {}
            except (TypeError, ValueError):
                continue
            if params.get("pool_name") == pool and params.get("trash_id"):
                result[params["trash_id"]] = action
    return result


def _latest_vm_perf_action() -> Action | None:
    with db.SessionLocal() as session:
        return (
            session.query(Action)
            .filter(Action.action_id == VM_PERF_BENCHMARK_ACTION_ID)
            .order_by(Action.created_at.desc())
            .first()
        )


def _volumes_page_context(
    request: Request,
    user: str,
    pool: str | None,
    pools: list[str],
    *,
    purge_error: str | None = None,
    purge_success: str | None = None,
    clusters: list | None = None,
    selected_cluster=None,
    selected_view: str = "pools",
) -> dict:
    """Shared by the GET page load and the "Xoá tất cả trash" POST below
    (which re-renders this same page directly rather than redirecting —
    unlike propose_rbd_trash_remove's redirect, a purge-all's own result
    has nothing left to look up after the fact via a GET, it only exists
    as this response's own purge_error/purge_success)."""
    trash_entries: list[dict] = []
    trash_pool_summaries: list[dict] = []
    trash_error: str | None = None
    trash_pending: dict[str, Action] = {}
    perf_sweep_action: Action | None = None
    vm_perf_action: Action | None = None
    if selected_view == "trash":
        cluster = _cluster_for_request(request)
        def fetch_trash(trash_pool: str):
            return (
                ceph_client.query_rbd_trash(trash_pool)
                if cluster.is_default
                else ceph_client.query_rbd_trash_with(trash_pool, *cluster_connection(cluster))
            )

        # A trash listing is one independent RBD command per pool. Bound the
        # fan-out so large installations do not create an unbounded number
        # of SSH sessions, while avoiding the old N x timeout page latency.
        results: dict[str, list[dict] | CephQueryError] = {}
        max_workers = min(8, max(1, len(pools)))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="trash-pool") as executor:
            futures = {executor.submit(fetch_trash, trash_pool): trash_pool for trash_pool in pools}
            for future in as_completed(futures):
                trash_pool = futures[future]
                try:
                    results[trash_pool] = future.result()
                except CephQueryError as exc:
                    results[trash_pool] = exc

        # Render in configured pool order even though requests completed out
        # of order, so parallelism never makes the UI jump around.
        for trash_pool in pools:
            result = results[trash_pool]
            if isinstance(result, CephQueryError):
                logger.warning("_volumes_page_context: failed to query trash for pool %r: %s", trash_pool, result)
                trash_error = f"{trash_pool}: {result}"
                trash_pool_summaries.append(
                    {
                        "pool": trash_pool,
                        "entry_count": None,
                        "total_used_size_bytes": None,
                        "total_used_size_human": "—",
                        "total_provisioned_size_bytes": None,
                        "total_provisioned_size_human": "—",
                        "error": str(result),
                    }
                )
                continue
            rows = result
            try:
                retention_rows = [_trash_retention(dict(row)) for row in rows]
                total_used_size_bytes = sum(max(0, int(row.get("used_size_bytes") or 0)) for row in rows)
                total_provisioned_size_bytes = sum(max(0, int(row.get("size_bytes") or 0)) for row in rows)
                trash_pool_summaries.append(
                    {
                        "pool": trash_pool,
                        "entry_count": len(rows),
                        "eligible_count": sum(1 for retention in retention_rows if retention["purge_eligible"]),
                        "total_used_size_bytes": total_used_size_bytes,
                        "total_used_size_human": _format_bytes(total_used_size_bytes),
                        "total_provisioned_size_bytes": total_provisioned_size_bytes,
                        "total_provisioned_size_human": _format_bytes(total_provisioned_size_bytes),
                        "error": None,
                    }
                )
                for row in rows:
                    if trash_pool != pool:
                        continue
                    item = dict(row)
                    item["pool"] = trash_pool
                    item["size_human"] = _format_bytes(item.get("size_bytes", 0))
                    item["used_size_human"] = _format_bytes(item.get("used_size_bytes", 0))
                    item.update(_trash_retention(item))
                    trash_entries.append(item)
            except (TypeError, ValueError) as exc:
                logger.warning("_volumes_page_context: invalid trash response for pool %r: %s", trash_pool, exc)
                trash_error = f"{trash_pool}: dữ liệu trash không hợp lệ"
        if cluster.is_default:
            for trash_pool in ([pool] if pool else []):
                for trash_id, action in _in_flight_trash_actions(trash_pool).items():
                    trash_pending[f"{trash_pool}/{trash_id}"] = action
    elif pool:
        if _cluster_for_request(request).is_default:
            perf_sweep_action = _latest_perf_sweep_action(pool)
            vm_perf_action = _latest_vm_perf_action()

    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "pools": pools,
        "selected_pool": pool,
        "selected_view": selected_view,
        "trash_entries": trash_entries,
        "trash_pool_summaries": trash_pool_summaries,
        "trash_error": trash_error,
        "trash_pending": trash_pending,
        "perf_sweep_action": perf_sweep_action,
        "vm_perf_action": vm_perf_action,
        "purge_error": purge_error,
        "purge_success": purge_success,
        "clusters": clusters or [],
        "selected_cluster": selected_cluster,
        "trash_retention_days": max(1, min(int(settings.rbd_trash_retention_days), 3650)),
    }


def _format_bytes(value: int | float) -> str:
    size = max(0.0, float(value or 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return "0 B"


def _trash_retention(entry: dict, *, now: datetime | None = None) -> dict:
    ttl_days = max(1, min(int(settings.rbd_trash_retention_days), 3650))
    raw = str(entry.get("deletion_time") or entry.get("deleted_at") or "").strip()
    try:
        deleted_at = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return {
            "purge_eligible": False, "expires_at": None,
            "retention_label": "Không xác định thời điểm xoá", "retention_days": ttl_days,
        }
    expires_at = deleted_at + timedelta(days=ttl_days)
    remaining_seconds = (expires_at - (now or datetime.utcnow())).total_seconds()
    remaining_days = max(0, int((remaining_seconds + 86399) // 86400))
    return {
        "purge_eligible": remaining_seconds <= 0,
        "expires_at": expires_at.isoformat() + "Z",
        "retention_label": "Đã hết TTL" if remaining_seconds <= 0 else f"Còn {remaining_days} ngày",
        "retention_days": ttl_days,
    }


@router.get("/volumes", response_class=HTMLResponse)
async def volumes_page(request: Request, user: str = Depends(require_login)):
    if request.query_params.get("view") == "trash":
        cluster_query = request.query_params.get("cluster", "").strip()
        suffix = f"?cluster={cluster_query}" if cluster_query else ""
        return RedirectResponse(url=f"/trash{suffix}", status_code=307)
    return await _volume_workspace_page(request, user, "volumes")


@router.get("/volume-performance", response_class=HTMLResponse)
async def volume_performance_page(request: Request, user: str = Depends(require_login)):
    return await _volume_workspace_page(request, user, "performance")


async def _volume_workspace_page(request: Request, user: str, selected_view: str):
    clusters, cluster = cluster_selection(request)
    pools = await asyncio.to_thread(_rbd_pools_for_request, request)
    requested_pool = request.query_params.get("pool")
    if requested_pool and requested_pool not in pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    # No default pool — same "must actually pick one" posture as
    # dashboard/routes/nodes.py's selected_host (landing on /volumes with
    # no ?pool= shows the empty "chọn một pool" state).
    return templates.TemplateResponse(
        request, "volumes.html", _volumes_page_context(
            request, user, requested_pool, pools, clusters=clusters, selected_cluster=cluster,
            selected_view=selected_view,
        )
    )


@router.get("/trash", response_class=HTMLResponse)
async def trash_page(request: Request, user: str = Depends(require_login)):
    """Top-level Pool navigation peer of /volumes and /pgs."""
    clusters, cluster = cluster_selection(request)
    pools = await asyncio.to_thread(_rbd_pools_for_request, request)
    requested_pool = request.query_params.get("pool", "").strip() or None
    if requested_pool and requested_pool not in pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    return templates.TemplateResponse(
        request,
        "volumes.html",
        _volumes_page_context(
            request,
            user,
            requested_pool,
            pools,
            clusters=clusters,
            selected_cluster=cluster,
            selected_view="trash",
        ),
    )


@router.get("/api/volumes/{pool}/iostat")
async def volume_iostat_api(request: Request, pool: str, user: str = Depends(require_login)):
    # `pool` is attacker-reachable input feeding into an `rbd` command run
    # over SSH — same SSRF-via-SSH whitelist posture as
    # dashboard/routes/nodes.py::node_metrics_api's `host` check. Only pools
    # the operator already configured (settings.ceph_rbd_pools) are queryable.
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    try:
        samples = ceph_client.query_rbd_iostat(pool) if cluster.is_default else ceph_client.query_rbd_iostat_with(
            pool, *cluster_connection(cluster)
        )
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
            .filter(_cluster_row_filter(Incident.cluster_id, cluster))
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


@router.get("/api/volumes/{pool}/images")
async def volume_known_images_api(request: Request, pool: str, user: str = Depends(require_login)):
    """Backs the volume-search box's autocomplete on the Volumes page —
    2026-07-29: that page used to list every volume's numbers directly, a
    live `rbd perf image iostat` table; it now asks the operator to search
    for one Volume by id/name instead, so this exists purely to suggest
    valid ids as they type. Combines TWO sources rather than either alone:
    VolumeMetric's distinct (pool, image) history (an image with zero
    recent I/O may not appear in a live iostat sample at all, but was
    still seen at some point and its history is still worth finding) UNION
    the current live iostat sample (a brand-new image that hasn't been
    through a Watcher poll yet — persist_last_poll_metrics only writes
    AFTER a poll completes — would otherwise be invisible here on its very
    first few seconds of life). Best-effort on the live half: a transient
    SSH failure here must not block finding a volume that already has
    plenty of persisted history."""
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    images: set[str] = set()
    with db.SessionLocal() as session:
        rows = session.query(VolumeMetric.image).filter(
            VolumeMetric.pool == pool, _cluster_row_filter(VolumeMetric.cluster_id, cluster)
        ).distinct().all()
        images.update(row[0] for row in rows)

    try:
        samples = ceph_client.query_rbd_iostat(pool) if cluster.is_default else ceph_client.query_rbd_iostat_with(
            pool, *cluster_connection(cluster)
        )
    except CephQueryError as exc:
        logger.warning("volume_known_images_api: live iostat failed, using history only: %s", exc)
    else:
        images.update(sample["image"] for sample in samples)

    return {"pool": pool, "images": sorted(images)}


@router.get("/api/volumes/{pool}/inventory")
async def volume_inventory_api(
    request: Request,
    pool: str,
    search: str = Query("", max_length=128),
    sort: str = Query("name"),
    order: str = Query("asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: str = Depends(require_login),
):
    """Live, read-only inventory from ``rbd du`` for the selected cluster."""
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    if sort not in {"name", "provisioned_size", "used_size", "snapshot_count"}:
        raise HTTPException(status_code=400, detail="Trường sắp xếp không hợp lệ")
    if order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="Thứ tự sắp xếp không hợp lệ")
    try:
        rows = (
            ceph_client.query_rbd_inventory(pool)
            if cluster.is_default
            else ceph_client.query_rbd_inventory_with(pool, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        logger.warning("volume_inventory_api: cluster=%s pool=%s: %s", cluster.id, pool, exc)
        raise HTTPException(status_code=502, detail=f"Không đọc được inventory RBD: {exc}")

    query = search.strip().casefold()
    if query:
        rows = [row for row in rows if query in row["name"].casefold()]
    rows.sort(key=lambda row: (row[sort] if sort != "name" else row["name"].casefold(), row["name"]))
    if order == "desc":
        rows.reverse()
    total = len(rows)
    start = (page - 1) * page_size
    all_provisioned = sum(int(row["provisioned_size"]) for row in rows)
    all_used = sum(int(row["used_size"]) for row in rows)
    return {
        "cluster_id": cluster.id,
        "pool": pool,
        "items": rows[start:start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "summary": {
            "image_count": total,
            "provisioned_size": all_provisioned,
            "used_size": all_used,
        },
        "pages": max(1, (total + page_size - 1) // page_size),
        "collected_at": datetime.utcnow().isoformat() + "Z",
    }


@router.get("/api/volumes/{pool}/inventory-overview")
async def volume_inventory_overview_api(
    request: Request, pool: str, user: str = Depends(require_login)
):
    """Live RBD pool durability and physical-capacity context."""
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    try:
        overview = (
            ceph_client.query_rbd_pool_overview(pool)
            if cluster.is_default
            else ceph_client.query_rbd_pool_overview_with(pool, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        logger.warning("volume_inventory_overview_api: cluster=%s pool=%s: %s", cluster.id, pool, exc)
        raise HTTPException(status_code=502, detail=f"Không đọc được tổng quan Pool: {exc}")
    return {"cluster_id": cluster.id, "collected_at": datetime.utcnow().isoformat() + "Z", **overview}


def _propose_rbd_volume_mutation(
    *, cluster, pool: str, image: str, action_id: str,
    ceph_code: str, user: str, rationale: str,
    extra_params: dict | None = None, conflicting_images: set[str] | None = None,
    idempotency_key: str | None = None,
) -> str:
    mon_nodes, _container, _ssh_user, _ssh_key, _exec_mode = cluster_connection(cluster)
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="Cluster chưa cấu hình MON node")
    params = {"pool_name": pool, "image": image, **(extra_params or {})}
    if idempotency_key:
        params["idempotency_key"] = idempotency_key
        params["requested_by"] = user
    conflict_names = conflicting_images or {image}
    try:
        preview = executor_commands.get_command(action_id, mon_nodes[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Thông tin Volume không hợp lệ: {exc}") from exc

    with db.SessionLocal() as session:
        in_flight = session.query(Action).filter(
            Action.action_id.in_(_RBD_VOLUME_MUTATION_ACTION_IDS),
            Action.status.in_(_IN_FLIGHT_ACTION_STATUSES),
        ).all()
        for existing in in_flight:
            try:
                existing_params = json.loads(existing.action_params or "{}")
            except (TypeError, ValueError):
                continue
            existing_names = {existing_params.get("image"), existing_params.get("new_image")}
            if existing_params.get("pool_name") == pool and conflict_names.intersection(existing_names):
                raise HTTPException(status_code=409, detail="Volume này đã có một thay đổi đang chờ duyệt hoặc thực thi")

        incident = Incident(
            cluster_id=cluster.id,
            ceph_code=ceph_code,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"{rationale} — yêu cầu bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=gate.classify_action(action_id).value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=rationale,
            target_nodes=json.dumps([mon_nodes[0]]),
            action_params=json.dumps(params),
            proposed_command=preview,
        )
        session.add(action)
        session.flush()
        audit.record(
            session, incident_id=incident.id, action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL, actor=user,
        )
        session.commit()
        return action.id


def _propose_cinder_attachment_mutation(
    *, cluster, pool: str, image: str, volume_id: str, server_id: str,
    action_id: str, ceph_code: str, user: str, idempotency_key: str | None,
) -> str:
    controllers = [node.strip() for node in cluster.openstack_controller_nodes.split(",") if node.strip()]
    if not controllers or not cluster.openstack_openrc_path:
        raise HTTPException(status_code=400, detail="Cluster chưa cấu hình OpenStack Controller/openrc")
    params = {
        "pool_name": pool, "image": image, "volume_id": volume_id,
        "server_id": server_id, "openrc_path": cluster.openstack_openrc_path,
        "requested_by": user,
    }
    if idempotency_key:
        params["idempotency_key"] = idempotency_key
    try:
        preview = executor_commands.get_command(action_id, controllers[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Thông tin Cinder attachment không hợp lệ: {exc}") from exc
    verb = "Gắn" if action_id == "cinder_attach_volume" else "Tháo"
    rationale = f"{verb} Cinder volume {volume_id} {'vào' if verb == 'Gắn' else 'khỏi'} Nova server {server_id}"
    with db.SessionLocal() as session:
        for existing in session.query(Action).filter(
            Action.action_id.in_(_RBD_VOLUME_MUTATION_ACTION_IDS),
            Action.status.in_(_IN_FLIGHT_ACTION_STATUSES),
        ).all():
            try:
                existing_params = json.loads(existing.action_params or "{}")
            except (TypeError, ValueError):
                continue
            if existing_params.get("pool_name") == pool and existing_params.get("image") == image:
                raise HTTPException(status_code=409, detail="Volume này đã có thay đổi đang chờ duyệt hoặc thực thi")
        incident = Incident(
            cluster_id=cluster.id, ceph_code=ceph_code,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"{rationale} — yêu cầu bởi {user}", detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id, action_id=action_id,
            classification=gate.classify_action(action_id).value,
            status=ActionStatus.PENDING_APPROVAL.value, rationale=rationale,
            target_nodes=json.dumps([controllers[0]]), action_params=json.dumps(params),
            proposed_command=preview,
        )
        session.add(action)
        session.flush()
        audit.record(
            session, incident_id=incident.id, action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL, actor=user,
        )
        session.commit()
        return action.id


def _propose_cinder_snapshot_create(
    *, cluster, pool: str, image: str, volume_id: str, snapshot_name: str,
    force: bool, user: str, idempotency_key: str | None,
) -> str:
    controllers = [node.strip() for node in cluster.openstack_controller_nodes.split(",") if node.strip()]
    if not controllers or not cluster.openstack_openrc_path:
        raise HTTPException(status_code=400, detail="Cluster chưa cấu hình OpenStack Controller/openrc")
    params = {
        "pool_name": pool, "image": image, "volume_id": volume_id,
        "snapshot_name": snapshot_name, "force": force,
        "openrc_path": cluster.openstack_openrc_path, "requested_by": user,
    }
    if idempotency_key:
        params["idempotency_key"] = idempotency_key
    try:
        preview = executor_commands.get_command("cinder_create_snapshot", controllers[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Thông tin Cinder snapshot không hợp lệ: {exc}") from exc
    rationale = (
        f"Tạo Cinder snapshot crash-consistent {snapshot_name} cho volume {volume_id}"
        + (" khi volume đang attached" if force else "")
    )
    with db.SessionLocal() as session:
        for existing in session.query(Action).filter(
            Action.action_id.in_(_RBD_VOLUME_MUTATION_ACTION_IDS),
            Action.status.in_(_IN_FLIGHT_ACTION_STATUSES),
        ).all():
            try:
                existing_params = json.loads(existing.action_params or "{}")
            except (TypeError, ValueError):
                continue
            if existing_params.get("pool_name") == pool and existing_params.get("image") == image:
                raise HTTPException(status_code=409, detail="Volume này đã có thay đổi đang chờ duyệt hoặc thực thi")
        incident = Incident(
            cluster_id=cluster.id, ceph_code=CINDER_SNAPSHOT_CREATE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"{rationale} — yêu cầu bởi {user}", detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id, action_id="cinder_create_snapshot",
            classification=gate.classify_action("cinder_create_snapshot").value,
            status=ActionStatus.PENDING_APPROVAL.value, rationale=rationale,
            target_nodes=json.dumps([controllers[0]]), action_params=json.dumps(params),
            proposed_command=preview,
        )
        session.add(action)
        session.flush()
        audit.record(
            session, incident_id=incident.id, action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL, actor=user,
        )
        session.commit()
        return action.id


def _idempotency_replay(
    request: Request, *, action_id: str, user: str, cluster_id: str, intent: dict,
) -> tuple[str | None, dict | None]:
    raw_key = request.headers.get("Idempotency-Key")
    if raw_key is None:
        return None, None
    key = raw_key.strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise HTTPException(status_code=400, detail="Idempotency-Key phải dài 8-128 ký tự an toàn")
    with db.SessionLocal() as session:
        rows = session.query(Action).all()
        for action in rows:
            try:
                params = json.loads(action.action_params or "{}")
            except (TypeError, ValueError):
                continue
            if params.get("idempotency_key") != key:
                continue
            incident = session.get(Incident, action.incident_id)
            same_intent = (
                action.action_id == action_id
                and params.get("requested_by") == user
                and incident is not None
                and incident.cluster_id == cluster_id
                and all(params.get(field) == value for field, value in intent.items())
            )
            if not same_intent:
                raise HTTPException(status_code=409, detail="Idempotency-Key đã được dùng cho yêu cầu khác")
            return key, {"action_id": action.id, "status": action.status, "replayed": True}
    return key, None


@router.post("/api/volumes/{pool}/inventory/create")
async def propose_volume_create(
    request: Request, pool: str, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    body = await request.json()
    image = str(body.get("image") or "").strip()
    if not _RBD_IMAGE_NAME_RE.fullmatch(image):
        raise HTTPException(status_code=400, detail="Tên Volume không hợp lệ")
    size_gib = body.get("size_gib")
    if isinstance(size_gib, bool) or not isinstance(size_gib, int) or not (1 <= size_gib <= 65536):
        raise HTTPException(status_code=400, detail="Dung lượng phải là số nguyên từ 1 đến 65536 GiB")
    idempotency_key, replay = _idempotency_replay(
        request, action_id="rbd_create_volume", user=user, cluster_id=cluster.id,
        intent={"pool_name": pool, "image": image, "size_mib": size_gib * 1024},
    )
    if replay:
        return replay
    try:
        inventory = (
            ceph_client.query_rbd_inventory(pool)
            if cluster.is_default
            else ceph_client.query_rbd_inventory_with(pool, *cluster_connection(cluster))
        )
        overview = (
            ceph_client.query_rbd_pool_overview(pool)
            if cluster.is_default
            else ceph_client.query_rbd_pool_overview_with(pool, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không chạy được preflight tạo Volume: {exc}")
    if any(row["name"] == image for row in inventory):
        raise HTTPException(status_code=409, detail="Volume đã tồn tại trong pool")
    requested_bytes = size_gib * 1024 ** 3
    if overview["max_available"] and requested_bytes > overview["max_available"]:
        raise HTTPException(status_code=409, detail="Dung lượng yêu cầu vượt max available của pool")
    action_pk = _propose_rbd_volume_mutation(
        cluster=cluster, pool=pool, image=image, extra_params={"size_mib": size_gib * 1024},
        action_id="rbd_create_volume", ceph_code=RBD_VOLUME_CREATE_CEPH_CODE, user=user,
        idempotency_key=idempotency_key,
        rationale=f"Tạo Volume {pool}/{image} dung lượng {size_gib} GiB trên cluster {cluster.name}",
    )
    return JSONResponse({"action_id": action_pk, "status": "PENDING_APPROVAL"}, status_code=201)


@router.post("/api/volumes/{pool}/inventory/{image}/resize")
async def propose_volume_resize(
    request: Request, pool: str, image: str, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    body = await request.json()
    if not _RBD_IMAGE_NAME_RE.fullmatch(image):
        raise HTTPException(status_code=400, detail="Tên Volume không hợp lệ")
    size_gib = body.get("size_gib")
    if isinstance(size_gib, bool) or not isinstance(size_gib, int) or not (1 <= size_gib <= 65536):
        raise HTTPException(status_code=400, detail="Dung lượng phải là số nguyên từ 1 đến 65536 GiB")
    idempotency_key, replay = _idempotency_replay(
        request, action_id="rbd_resize_volume", user=user, cluster_id=cluster.id,
        intent={"pool_name": pool, "image": image, "size_mib": size_gib * 1024},
    )
    if replay:
        return replay
    try:
        detail = (
            ceph_client.query_rbd_image_detail(pool, image)
            if cluster.is_default
            else ceph_client.query_rbd_image_detail_with(pool, image, *cluster_connection(cluster))
        )
        overview = (
            ceph_client.query_rbd_pool_overview(pool)
            if cluster.is_default
            else ceph_client.query_rbd_pool_overview_with(pool, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không chạy được preflight resize Volume: {exc}")
    requested_bytes = size_gib * 1024 ** 3
    current_bytes = int(detail.get("size") or 0)
    if requested_bytes <= current_bytes:
        raise HTTPException(status_code=409, detail="Resize chỉ hỗ trợ mở rộng; dung lượng mới phải lớn hơn hiện tại")
    if overview["max_available"] and requested_bytes - current_bytes > overview["max_available"]:
        raise HTTPException(status_code=409, detail="Phần dung lượng tăng thêm vượt max available của pool")
    action_pk = _propose_rbd_volume_mutation(
        cluster=cluster, pool=pool, image=image, extra_params={"size_mib": size_gib * 1024},
        action_id="rbd_resize_volume", ceph_code=RBD_VOLUME_RESIZE_CEPH_CODE, user=user,
        idempotency_key=idempotency_key,
        rationale=f"Mở rộng Volume {pool}/{image} từ {_format_bytes(current_bytes)} lên {size_gib} GiB",
    )
    return JSONResponse({"action_id": action_pk, "status": "PENDING_APPROVAL"}, status_code=201)


@router.post("/api/volumes/{pool}/inventory/{image}/rename")
async def propose_volume_rename(
    request: Request, pool: str, image: str, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    body = await request.json()
    new_image = str(body.get("new_image") or "").strip()
    if not _RBD_IMAGE_NAME_RE.fullmatch(image) or not _RBD_IMAGE_NAME_RE.fullmatch(new_image):
        raise HTTPException(status_code=400, detail="Tên Volume nguồn hoặc đích không hợp lệ")
    if new_image == image:
        raise HTTPException(status_code=409, detail="Tên mới phải khác tên hiện tại")
    idempotency_key, replay = _idempotency_replay(
        request, action_id="rbd_rename_volume", user=user, cluster_id=cluster.id,
        intent={"pool_name": pool, "image": image, "new_image": new_image},
    )
    if replay:
        return replay
    try:
        detail = (
            ceph_client.query_rbd_image_detail(pool, image)
            if cluster.is_default
            else ceph_client.query_rbd_image_detail_with(pool, image, *cluster_connection(cluster))
        )
        inventory = (
            ceph_client.query_rbd_inventory(pool)
            if cluster.is_default
            else ceph_client.query_rbd_inventory_with(pool, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không chạy được preflight rename Volume: {exc}")
    if any(row.get("name") == new_image for row in inventory):
        raise HTTPException(status_code=409, detail="Tên Volume đích đã tồn tại trong pool")
    if detail.get("watchers"):
        raise HTTPException(status_code=409, detail="Phải detach Volume khỏi mọi consumer trước khi rename")
    action_pk = _propose_rbd_volume_mutation(
        cluster=cluster, pool=pool, image=image, extra_params={"new_image": new_image},
        conflicting_images={image, new_image}, action_id="rbd_rename_volume",
        ceph_code=RBD_VOLUME_RENAME_CEPH_CODE, user=user,
        idempotency_key=idempotency_key,
        rationale=f"Đổi tên Volume {pool}/{image} thành {pool}/{new_image}; thao tác yêu cầu downtime",
    )
    return JSONResponse({"action_id": action_pk, "status": "PENDING_APPROVAL"}, status_code=201)


@router.post("/api/volumes/{pool}/inventory/{image}/trash")
async def propose_volume_trash_move(
    request: Request, pool: str, image: str, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    if not _RBD_IMAGE_NAME_RE.fullmatch(image):
        raise HTTPException(status_code=400, detail="Tên Volume không hợp lệ")
    idempotency_key, replay = _idempotency_replay(
        request, action_id="rbd_trash_move_volume", user=user, cluster_id=cluster.id,
        intent={"pool_name": pool, "image": image},
    )
    if replay:
        return replay
    try:
        detail = (
            ceph_client.query_rbd_image_detail(pool, image)
            if cluster.is_default
            else ceph_client.query_rbd_image_detail_with(pool, image, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không chạy được preflight chuyển Trash: {exc}")
    blockers = []
    if detail.get("watchers"):
        blockers.append("watcher/attachment")
    if detail.get("snapshots"):
        blockers.append("snapshot")
    if detail.get("children"):
        blockers.append("clone child")
    with db.SessionLocal() as session:
        running_backup = (
            session.query(BackupJob.id)
            .filter(
                _cluster_row_filter(BackupJob.cluster_id, cluster),
                BackupJob.pool == pool,
                BackupJob.image == image,
                BackupJob.status == "RUNNING",
            )
            .first()
        )
    if running_backup:
        blockers.append("backup đang chạy")
    if blockers:
        raise HTTPException(status_code=409, detail="Không thể chuyển Trash khi còn dependency: " + ", ".join(blockers))
    action_pk = _propose_rbd_volume_mutation(
        cluster=cluster, pool=pool, image=image, action_id="rbd_trash_move_volume",
        ceph_code=RBD_VOLUME_TRASH_MOVE_CEPH_CODE, user=user,
        idempotency_key=idempotency_key,
        rationale=f"Chuyển mềm Volume {pool}/{image} vào RBD Trash để có thể khôi phục",
    )
    return JSONResponse({"action_id": action_pk, "status": "PENDING_APPROVAL"}, status_code=201)


@router.post("/api/volumes/{pool}/trash/{trash_id}/restore")
async def propose_volume_trash_restore(
    request: Request, pool: str, trash_id: str, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    body = await request.json()
    image = str(body.get("image") or "").strip()
    if not _RBD_IMAGE_NAME_RE.fullmatch(image):
        raise HTTPException(status_code=400, detail="Tên Volume khôi phục không hợp lệ")
    idempotency_key, replay = _idempotency_replay(
        request, action_id="rbd_trash_restore_volume", user=user, cluster_id=cluster.id,
        intent={"pool_name": pool, "image": image, "trash_id": trash_id},
    )
    if replay:
        return replay
    try:
        trash = (
            ceph_client.query_rbd_trash(pool)
            if cluster.is_default
            else ceph_client.query_rbd_trash_with(pool, *cluster_connection(cluster))
        )
        inventory = (
            ceph_client.query_rbd_inventory(pool)
            if cluster.is_default
            else ceph_client.query_rbd_inventory_with(pool, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không chạy được preflight restore Trash: {exc}")
    if not any(str(entry.get("id")) == trash_id for entry in trash):
        raise HTTPException(status_code=404, detail="Trash ID không còn tồn tại trong pool")
    if any(entry.get("name") == image for entry in inventory):
        raise HTTPException(status_code=409, detail="Tên Volume khôi phục đã tồn tại trong pool")
    action_pk = _propose_rbd_volume_mutation(
        cluster=cluster, pool=pool, image=image,
        extra_params={"trash_id": trash_id}, action_id="rbd_trash_restore_volume",
        ceph_code=RBD_VOLUME_TRASH_RESTORE_CEPH_CODE, user=user,
        idempotency_key=idempotency_key,
        rationale=f"Khôi phục Trash {pool}/{trash_id} thành Volume {pool}/{image}",
    )
    return JSONResponse({"action_id": action_pk, "status": "PENDING_APPROVAL"}, status_code=201)


@router.get("/api/volumes/{pool}/inventory/{image}")
async def volume_inventory_detail_api(
    request: Request, pool: str, image: str, user: str = Depends(require_login)
):
    """Live image metadata and dependency graph; never mutates the image."""
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    if not image or len(image) > 128 or "\x00" in image or "/" in image:
        raise HTTPException(status_code=400, detail="Tên Volume không hợp lệ")
    try:
        detail = (
            ceph_client.query_rbd_image_detail(pool, image)
            if cluster.is_default
            else ceph_client.query_rbd_image_detail_with(pool, image, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        logger.warning("volume_inventory_detail_api: cluster=%s volume=%s/%s: %s", cluster.id, pool, image, exc)
        raise HTTPException(status_code=502, detail=f"Không đọc được chi tiết Volume: {exc}")
    cinder = await asyncio.to_thread(discover_cinder_volume, cluster, image)
    detail["cinder"] = cinder
    detail["cinder_snapshots"] = (
        await asyncio.to_thread(discover_cinder_snapshots, cluster, str(cinder.get("volume_id")))
        if cinder.get("status") == "managed" and cinder.get("verified")
        else {"status": "not_applicable", "items": [], "count": 0}
    )
    reconciliation = reconcile_cinder_attachment(
        cinder, detail.get("watchers") or [], detail.get("locks") or []
    )
    detail["attachment_reconciliation"] = reconciliation
    if cinder.get("verified"):
        summary = detail.setdefault("attachment_summary", {})
        summary["management_source"] = "openstack_cinder"
        summary["cinder_verified"] = True
        summary["consumer_count"] = len(cinder.get("attachments") or [])
        summary["mutation_supported"] = False
        summary["blocked_reason"] = "Attach/detach qua Cinder chưa được bật."
    if not reconciliation.get("safe"):
        detail.setdefault("attachment_summary", {})["mutation_supported"] = False
        detail["attachment_summary"]["blocked_reason"] = reconciliation.get("reason") or "Attachment chưa đối soát an toàn."
    return {"cluster_id": cluster.id, "collected_at": datetime.utcnow().isoformat() + "Z", **detail}


async def _cinder_attachment_preflight(cluster, pool: str, image: str) -> tuple[dict, dict, dict]:
    try:
        detail = (
            ceph_client.query_rbd_image_detail(pool, image)
            if cluster.is_default
            else ceph_client.query_rbd_image_detail_with(pool, image, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không đọc được Ceph attachment evidence: {exc}")
    cinder = await asyncio.to_thread(discover_cinder_volume, cluster, image)
    reconciliation = reconcile_cinder_attachment(
        cinder, detail.get("watchers") or [], detail.get("locks") or []
    )
    if cinder.get("status") != "managed" or not cinder.get("verified"):
        raise HTTPException(status_code=409, detail="Volume chưa được xác minh là tài nguyên Cinder")
    if reconciliation.get("status") != "healthy":
        raise HTTPException(
            status_code=409,
            detail="Attachment chưa đối soát an toàn: " + str(reconciliation.get("reason") or "unknown"),
        )
    return detail, cinder, reconciliation


def _cinder_volume_id_from_image(image: str) -> str:
    volume_id = image.removeprefix("volume-") if image.startswith("volume-") else ""
    if not _OPENSTACK_UUID_RE.fullmatch(volume_id):
        raise HTTPException(status_code=400, detail="RBD image không theo định dạng Cinder volume-<UUID>")
    return volume_id


@router.post("/api/volumes/{pool}/inventory/{image}/attach")
async def propose_cinder_volume_attach(
    request: Request, pool: str, image: str, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    body = await request.json()
    server_id = str(body.get("server_id") or "").strip()
    if not _OPENSTACK_UUID_RE.fullmatch(server_id):
        raise HTTPException(status_code=400, detail="Nova server ID không hợp lệ")
    volume_id = _cinder_volume_id_from_image(image)
    intent = {"pool_name": pool, "image": image, "volume_id": volume_id, "server_id": server_id}
    key, replay = _idempotency_replay(
        request, action_id="cinder_attach_volume", user=user, cluster_id=cluster.id, intent=intent,
    )
    if replay:
        return replay
    _detail, cinder, _reconciliation = await _cinder_attachment_preflight(cluster, pool, image)
    attachments = cinder.get("attachments") if isinstance(cinder.get("attachments"), list) else []
    if any(str(item.get("instance_id") or "").lower() == server_id.lower() for item in attachments):
        raise HTTPException(status_code=409, detail="Nova server đã attach Cinder volume này")
    first_attach = cinder.get("volume_status") == "available" and not attachments
    shared_attach = (
        cinder.get("volume_status") == "in-use"
        and bool(cinder.get("multiattach"))
        and bool(attachments)
    )
    if not (first_attach or shared_attach):
        raise HTTPException(
            status_code=409,
            detail="Volume đang in-use chỉ được attach thêm khi Cinder xác nhận multiattach=true",
        )
    if str(cinder["volume_id"]).lower() != volume_id.lower():
        raise HTTPException(status_code=409, detail="Cinder volume ID không khớp RBD image")
    action_id = _propose_cinder_attachment_mutation(
        cluster=cluster, pool=pool, image=image, volume_id=volume_id, server_id=server_id,
        action_id="cinder_attach_volume", ceph_code=CINDER_VOLUME_ATTACH_CEPH_CODE,
        user=user, idempotency_key=key,
    )
    return JSONResponse({"action_id": action_id, "status": "PENDING_APPROVAL"}, status_code=201)


@router.post("/api/volumes/{pool}/inventory/{image}/detach")
async def propose_cinder_volume_detach(
    request: Request, pool: str, image: str, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    body = await request.json()
    server_id = str(body.get("server_id") or "").strip()
    if not _OPENSTACK_UUID_RE.fullmatch(server_id):
        raise HTTPException(status_code=400, detail="Nova server ID không hợp lệ")
    volume_id = _cinder_volume_id_from_image(image)
    intent = {"pool_name": pool, "image": image, "volume_id": volume_id, "server_id": server_id}
    key, replay = _idempotency_replay(
        request, action_id="cinder_detach_volume", user=user, cluster_id=cluster.id, intent=intent,
    )
    if replay:
        return replay
    _detail, cinder, _reconciliation = await _cinder_attachment_preflight(cluster, pool, image)
    if not any(str(item.get("instance_id") or "") == server_id for item in cinder.get("attachments") or []):
        raise HTTPException(status_code=409, detail="Nova server không có attachment trên Cinder volume này")
    if str(cinder["volume_id"]).lower() != volume_id.lower():
        raise HTTPException(status_code=409, detail="Cinder volume ID không khớp RBD image")
    action_id = _propose_cinder_attachment_mutation(
        cluster=cluster, pool=pool, image=image, volume_id=volume_id, server_id=server_id,
        action_id="cinder_detach_volume", ceph_code=CINDER_VOLUME_DETACH_CEPH_CODE,
        user=user, idempotency_key=key,
    )
    return JSONResponse({"action_id": action_id, "status": "PENDING_APPROVAL"}, status_code=201)


@router.post("/api/volumes/{pool}/inventory/{image}/snapshots")
async def propose_cinder_snapshot_create(
    request: Request, pool: str, image: str, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    body = await request.json()
    snapshot_name = str(body.get("snapshot_name") or "").strip()
    if not _CINDER_SNAPSHOT_NAME_RE.fullmatch(snapshot_name):
        raise HTTPException(status_code=400, detail="Tên Cinder snapshot không hợp lệ")
    volume_id = _cinder_volume_id_from_image(image)
    intent = {
        "pool_name": pool, "image": image, "volume_id": volume_id,
        "snapshot_name": snapshot_name,
    }
    key, replay = _idempotency_replay(
        request, action_id="cinder_create_snapshot", user=user,
        cluster_id=cluster.id, intent=intent,
    )
    if replay:
        return replay
    _detail, cinder, _reconciliation = await _cinder_attachment_preflight(cluster, pool, image)
    if str(cinder.get("volume_id") or "").lower() != volume_id.lower():
        raise HTTPException(status_code=409, detail="Cinder volume ID không khớp RBD image")
    snapshot_inventory = await asyncio.to_thread(discover_cinder_snapshots, cluster, volume_id)
    if snapshot_inventory.get("status") != "ok":
        raise HTTPException(
            status_code=502,
            detail="Không kiểm tra được snapshot hiện có: " + str(snapshot_inventory.get("error") or "unknown"),
        )
    if any(str(item.get("name") or "") == snapshot_name for item in snapshot_inventory.get("items") or []):
        raise HTTPException(status_code=409, detail="Tên Cinder snapshot đã tồn tại")
    force = cinder.get("volume_status") == "in-use"
    action_id = _propose_cinder_snapshot_create(
        cluster=cluster, pool=pool, image=image, volume_id=volume_id,
        snapshot_name=snapshot_name, force=force, user=user, idempotency_key=key,
    )
    return JSONResponse({"action_id": action_id, "status": "PENDING_APPROVAL"}, status_code=201)


@router.get("/api/volumes/{pool}/{image}/history")
async def volume_history_api(
    request: Request, pool: str, image: str, hours: int = _DEFAULT_HISTORY_HOURS,
    user: str = Depends(require_login)
):
    """Powers the performance chart on the Volumes page — persisted
    VolumeMetric history for exactly one (pool, image), not a live SSH
    query (see VolumeMetric's own docstring: it's written once per Watcher
    poll for every sample, independent of whatever the dashboard is doing).
    `hours` bounds only the plotted time-series (clamped to
    _MAX_HISTORY_HOURS — see that constant's own comment); `peak` below is
    deliberately computed over the table's ENTIRE retained history for this
    volume regardless of `hours`, since "hiệu năng tối đa từng đạt được"
    (peak performance ever achieved) must survive collapsing/scrolling the
    visible chart window."""
    cluster, allowed_pools = _allowed_pools_for_request(request)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    hours = max(1, min(hours, _MAX_HISTORY_HOURS))
    since = datetime.utcnow() - timedelta(hours=hours)

    with db.SessionLocal() as session:
        rows = (
            session.query(VolumeMetric)
            .filter(_cluster_row_filter(VolumeMetric.cluster_id, cluster), VolumeMetric.pool == pool, VolumeMetric.image == image)
            .filter(VolumeMetric.polled_at >= since)
            .order_by(VolumeMetric.polled_at.asc())
            .all()
        )
        samples = [
            {
                # "Z" appended (2026-07-29): polled_at is stored naive-UTC
                # (datetime.utcnow(), same convention as every other
                # timestamped table in this app) — without an explicit UTC
                # marker, JS `new Date(isoString)` parses a bare
                # "YYYY-MM-DDTHH:MM:SS" as LOCAL time instead, silently
                # shifting every point on the chart by the browser's UTC
                # offset.
                "polled_at": row.polled_at.isoformat() + "Z",
                "iops": row.iops,
                "read_latency_ms": row.read_latency_ms,
                "write_latency_ms": row.write_latency_ms,
                "saturated": row.saturated,
            }
            for row in rows
        ]

        def _peak(field: str) -> dict | None:
            row = (
                session.query(VolumeMetric)
                .filter(_cluster_row_filter(VolumeMetric.cluster_id, cluster), VolumeMetric.pool == pool, VolumeMetric.image == image)
                .order_by(getattr(VolumeMetric, field).desc())
                .first()
            )
            if row is None:
                return None
            return {"value": getattr(row, field), "at": row.polled_at.isoformat() + "Z"}

        peak = {
            "iops": _peak("iops"),
            "read_latency_ms": _peak("read_latency_ms"),
            "write_latency_ms": _peak("write_latency_ms"),
        }

        # Same authoritative-Incident-table posture as volume_iostat_api
        # above, rather than trusting the last sample's own `saturated`
        # column (see VolumeMetric's own docstring for why that column is
        # a close-but-not-authoritative proxy).
        saturated_now = (
            session.query(Incident)
            .filter(_cluster_row_filter(Incident.cluster_id, cluster), Incident.ceph_code == ceph_code_for(pool, image))
            .filter(Incident.status.in_(OPEN_STATUSES))
            .first()
            is not None
        )

    return {
        "pool": pool,
        "image": image,
        "hours": hours,
        "samples": samples,
        "peak": peak,
        "saturated": saturated_now,
    }


# --- End-to-end VM disk benchmark -----------------------------------------


@router.post("/volumes/vm-perf/propose")
async def propose_vm_perf_benchmark(request: Request, user: str = Depends(require_login)):
    """Create an approval-gated, read-only fio benchmark inside one VM."""
    _require_admin_privilege(user)
    cluster = _require_default_cluster_operation(request)
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Dữ liệu yêu cầu không hợp lệ")

    vm_ip = str(payload.get("vm_ip") or "").strip()
    ssh_user = str(payload.get("ssh_user") or "").strip()
    ssh_key_path = str(payload.get("ssh_key_path") or "").strip()
    device = str(payload.get("device") or "").strip()
    try:
        ipaddress.ip_address(vm_ip)
    except ValueError:
        raise HTTPException(status_code=400, detail="IP của VM không hợp lệ")
    if not _SSH_USER_RE.fullmatch(ssh_user):
        raise HTTPException(status_code=400, detail="SSH user không hợp lệ")
    if not _VM_DEVICE_RE.fullmatch(device):
        raise HTTPException(status_code=400, detail="Ổ đĩa phải có dạng /dev/vdb, /dev/vdc, ...")

    if not ssh_key_path.startswith("/") or "\x00" in ssh_key_path or "\n" in ssh_key_path:
        raise HTTPException(
            status_code=400,
            detail="SSH key phải là đường dẫn tuyệt đối tới private key của VM trên OpenStack Controller",
        )
    controller_nodes = [node.strip() for node in cluster.openstack_controller_nodes.split(",") if node.strip()]
    if not controller_nodes:
        raise HTTPException(status_code=400, detail="Cluster chưa cấu hình OpenStack Controller")
    controller_ip = controller_nodes[0]

    previous = _latest_vm_perf_action()
    if previous is not None and previous.status in _IN_FLIGHT_ACTION_STATUSES:
        raise HTTPException(status_code=409, detail="Đã có một lượt đo VM đang chờ duyệt hoặc đang chạy")

    params = {
        "vm_ip": vm_ip,
        "controller_ip": controller_ip,
        "ssh_user": ssh_user,
        "ssh_key_path": ssh_key_path,
        "device": device,
        "requested_by": user,
    }
    preview = executor_commands.get_command(VM_PERF_BENCHMARK_ACTION_ID, vm_ip, params)
    with db.SessionLocal() as session:
        incident = Incident(
            cluster_id=cluster.id,
            ceph_code=VM_PERF_BENCHMARK_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=(
                f"Đề xuất đo read-only từ Controller {controller_ip} qua VM {vm_ip}, "
                f"ổ {device}, bởi {user}."
            ),
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=VM_PERF_BENCHMARK_ACTION_ID,
            classification=gate.classify_action(VM_PERF_BENCHMARK_ACTION_ID).value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"SSH vào Controller {controller_ip}, từ đó SSH vào VM {vm_ip} và chạy fio 4K "
                f"random-read trên {device}. Phép đo không ghi dữ liệu "
                "nhưng tạo tải đọc thật, có thể làm chậm workload đang chạy."
            ),
            target_nodes=json.dumps([controller_ip]),
            action_params=json.dumps(params),
            proposed_command=preview,
        )
        session.add(action)
        session.flush()
        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()
        action_id = action.id
    return JSONResponse({"action_id": action_id}, status_code=201)


@router.get("/api/volumes/vm-perf/progress")
async def vm_perf_progress_api(user: str = Depends(require_login)):
    action = _latest_vm_perf_action()
    if action is None:
        return {"status": None, "progress": []}
    try:
        progress = json.loads(action.execution_progress) if action.execution_progress else []
    except (TypeError, ValueError):
        progress = []
    return {"status": action.status, "progress": _with_step_display_times(progress)}


# --- "Đo hiệu năng tối đa" load sweep (2026-07-29) ------------------------


def _latest_perf_sweep_action(pool: str) -> Action | None:
    """Most recent volume_perf_sweep Action for `pool`, regardless of
    status — same "load every row of a small table, filter action_params
    in Python" posture as _in_flight_trash_actions above (no portable
    cross-DB JSON-field query in this codebase)."""
    with db.SessionLocal() as session:
        rows = (
            session.query(Action)
            .filter(Action.action_id == VOLUME_PERF_SWEEP_ACTION_ID)
            .order_by(Action.created_at.desc())
            .all()
        )
        for action in rows:
            try:
                params = json.loads(action.action_params) if action.action_params else {}
            except (TypeError, ValueError):
                continue
            if params.get("pool") == pool:
                return action
    return None


def _step_clock(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return format_vn_clock(datetime.fromisoformat(value))
    except ValueError:
        return None


def _with_step_display_times(progress: list) -> list:
    """Same fix, same reason as dashboard/routes/convert_cluster.py's
    identical helper — freezes a finished step's displayed time instead of
    letting the frontend drift it to "now" on every poll."""
    for step in progress:
        if isinstance(step, dict):
            step["started_at_display"] = _step_clock(step.get("started_at"))
            step["finished_at_display"] = _step_clock(step.get("finished_at"))
    return progress


@router.post("/volumes/{pool}/perf-sweep/propose")
async def propose_volume_perf_sweep(request: Request, pool: str, user: str = Depends(require_login)):
    """"Đo hiệu năng tối đa (Load sweep)" button — admin-gated (same
    posture as purge_all_rbd_trash below): unlike the per-image Trash
    "Xoá" button, this WRITES REAL I/O LOAD to the cluster for several
    minutes once approved, so the bar for who can even propose it is
    higher than this app's usual "any logged-in operator" default for a
    propose-then-approve action.

    Always targets a dedicated scratch image (see worker/executor/
    volume_perf.py's own module docstring) — never accepts an `image`
    parameter from the request, so there is no way to point this at a
    real volume even by a crafted request."""
    _require_admin_privilege(user)
    _require_default_cluster_operation(request)
    allowed_pools = set(ceph_client.configured_rbd_pools())
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    existing = _latest_perf_sweep_action(pool)
    if existing is not None and existing.status in _IN_FLIGHT_ACTION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Đã có một lượt đo hiệu năng đang chờ duyệt hoặc đang chạy cho pool này.",
        )

    mon_nodes = ceph_client.get_mon_nodes()
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="Chưa cấu hình CEPH_MON_NODES")
    osd_ips = [h.strip() for h in settings.ceph_osd_nodes.split(",") if h.strip()]

    action_params = {
        "pool": pool,
        "mon_ip": mon_nodes[0],
        "osd_ips": osd_ips,
        "requested_by": user,
    }
    try:
        preview_command = executor_commands.get_command(
            VOLUME_PERF_SWEEP_ACTION_ID, mon_nodes[0], action_params
        )
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        incident = Incident(
            ceph_code=VOLUME_PERF_SWEEP_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=(
                f"Đề xuất đo hiệu năng tối đa (load sweep) cho pool {pool} bởi {user} — dùng "
                f"scratch image riêng, không đụng volume thật."
            ),
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()

        action = Action(
            incident_id=incident.id,
            action_id=VOLUME_PERF_SWEEP_ACTION_ID,
            classification=gate.classify_action(VOLUME_PERF_SWEEP_ACTION_ID).value,  # always RISKY (AD-5)
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"Quét tải fio tăng dần (iodepth 1→256) trên scratch image riêng trong pool {pool} "
                f"để tìm điểm bão hoà IOPS ghi ngẫu nhiên 4K — không đụng tới volume thật, nhưng tạo "
                f"tải I/O thật lên cluster khoảng 10–20 phút khi chạy (có thể lâu hơn nếu mẫu nhiễu)."
            ),
            target_nodes=json.dumps([mon_nodes[0]]),
            action_params=json.dumps(action_params),
            proposed_command=preview_command,
        )
        session.add(action)
        session.flush()

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()
        action_pk = action.id

    return JSONResponse({"action_id": action_pk}, status_code=201)


@router.get("/api/volumes/{pool}/perf-sweep/progress")
async def volume_perf_sweep_progress_api(pool: str, user: str = Depends(require_login)):
    action = _latest_perf_sweep_action(pool)
    if action is None:
        return {"status": None, "progress": []}
    try:
        progress = json.loads(action.execution_progress) if action.execution_progress else []
    except (TypeError, ValueError):
        progress = []
    progress = _with_step_display_times(progress)
    return {"status": action.status, "progress": progress}


def _serialize_perf_sweep(row: VolumePerfSweep) -> dict:
    try:
        steps = json.loads(row.steps_json) if row.steps_json else []
    except (TypeError, ValueError):
        steps = []
    knee = None
    if row.knee_iodepth is not None:
        knee = {
            "iodepth": row.knee_iodepth,
            "iops": row.knee_iops,
            "latency_avg_ms": row.knee_latency_avg_ms,
            "latency_p99_ms": row.knee_latency_p99_ms,
        }
    ai_conclusion = None
    if row.ai_conclusion:
        try:
            ai_conclusion = json.loads(row.ai_conclusion)
        except (TypeError, ValueError):
            ai_conclusion = None
    return {
        "status": row.status,
        "scratch_image": row.scratch_image,
        "requested_by": row.requested_by,
        "steps": steps,
        "knee": knee,
        "qos_notes": row.qos_notes,
        "bottleneck_notes": row.bottleneck_notes,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() + "Z",
        "finished_at": (row.finished_at.isoformat() + "Z") if row.finished_at else None,
        "ai_conclusion": ai_conclusion,
        "ai_analyzed_at": (row.ai_analyzed_at.isoformat() + "Z") if row.ai_analyzed_at else None,
    }


@router.get("/api/volumes/{pool}/perf-sweep/latest")
async def volume_perf_sweep_latest_api(pool: str, user: str = Depends(require_login)):
    """Latest COMPLETED sweep result for `pool`, regardless of which
    Action produced it — the Volumes page's summary panel reads this
    directly rather than re-deriving it from Action.execution_progress
    (that JSON is the LIVE view of a run in progress; this table is the
    durable result left behind afterward, see VolumePerfSweep's own
    docstring)."""
    allowed_pools = set(ceph_client.configured_rbd_pools())
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    with db.SessionLocal() as session:
        row = (
            session.query(VolumePerfSweep)
            .filter(VolumePerfSweep.pool == pool)
            .order_by(VolumePerfSweep.created_at.desc())
            .first()
        )
        if row is None:
            return {"pool": pool, "sweep": None}
        return {"pool": pool, "sweep": _serialize_perf_sweep(row)}


@router.post("/api/volumes/{pool}/perf-sweep/analyze")
async def volume_perf_sweep_analyze_api(pool: str, user: str = Depends(require_login)):
    """"Phân tích bằng AI" button — sends the latest COMPLETED sweep's
    evidence (steps/knee/QoS/bottleneck notes) to the operator's configured
    router (dashboard/volume_perf_analysis.py) for a plain-language final
    conclusion, augmenting (not replacing) _detect_knee's own algorithmic
    result. Not admin-gated, unlike the propose route above — this is
    read-only analysis of already-collected data (no SSH, no cluster
    mutation), same accessibility as Chat-with-AI."""
    allowed_pools = set(ceph_client.configured_rbd_pools())
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    with db.SessionLocal() as session:
        row = (
            session.query(VolumePerfSweep)
            .filter(VolumePerfSweep.pool == pool)
            .order_by(VolumePerfSweep.created_at.desc())
            .first()
        )
        if row is None or row.status != "DONE":
            raise HTTPException(
                status_code=400,
                detail="Chưa có kết quả đo hiệu năng nào hoàn tất cho pool này để phân tích.",
            )
        sweep_payload = _serialize_perf_sweep(row)
        sweep_payload["pool"] = pool
        row_id = row.id

    try:
        conclusion = await volume_perf_analysis.analyze_volume_perf_sweep(sweep_payload)
    except volume_perf_analysis.VolumePerfAnalysisError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    with db.SessionLocal() as session:
        row = session.get(VolumePerfSweep, row_id)
        if row is not None:
            row.ai_conclusion = json.dumps(conclusion)
            row.ai_analyzed_at = datetime.utcnow()
            session.commit()

    return {"pool": pool, "conclusion": conclusion}


@router.post("/volumes/{pool}/trash/{trash_id}/propose")
async def propose_rbd_trash_remove(request: Request, pool: str, trash_id: str, user: str = Depends(require_login)):
    """"Xoá" button on the Volumes page's Trash section — creates a
    PENDING_APPROVAL Action the operator must separately approve (via the
    already-generic POST /actions/{id}/approve — no new approval logic
    needed), same propose-then-approve pattern as dashboard/routes/
    delete_cluster.py/upgrade.py/deploy_cluster.py. Always PENDING_APPROVAL
    regardless of rbd_trash_remove's own SAFE/RISKY classification (RISKY,
    see action_policy.yaml) — same as those other dedicated-route features,
    none of which auto-execute even a SAFE action_id; only Chat-with-AI's
    confirm flow does that.
    """
    _require_default_cluster_operation(request)
    allowed_pools = set(ceph_client.configured_rbd_pools())
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")
    try:
        entries = ceph_client.query_rbd_trash(pool)
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không kiểm tra được TTL Trash: {exc}")
    entry = next((row for row in entries if str(row.get("id")) == trash_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Trash ID không còn tồn tại trong pool")
    retention = _trash_retention(dict(entry))
    if not retention["purge_eligible"]:
        raise HTTPException(status_code=409, detail=f"Volume chưa hết TTL: {retention['retention_label']}")

    # 2026-07-28 fix: this used to be target_nodes=[] — worker/llm/
    # router_client.py::_execute_approved_action requires a NON-EMPTY host
    # list (dashboard/chat_client.py: "management action_ids, target_nodes
    # must contain exactly ONE host") and marks the Action FAILED outright
    # otherwise, without ever attempting the command. Approving this Action
    # therefore always failed, silently — verified by reading, not by a
    # user report, so treat this as unconfirmed against a real approval
    # until one actually succeeds against a live cluster. rbd_trash_remove
    # is a single global command (like ceph osd pool delete/create), not a
    # per-host one — exactly one MON, no fan-out, same convention.
    mon_nodes = ceph_client.get_mon_nodes()
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="Chưa cấu hình CEPH_MON_NODES")

    action_params = {"pool_name": pool, "trash_id": trash_id}
    try:
        preview_command = executor_commands.get_command(RBD_TRASH_REMOVE_ACTION_ID, mon_nodes[0], action_params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        existing = (
            session.query(Action)
            .filter(Action.action_id == RBD_TRASH_REMOVE_ACTION_ID)
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .all()
        )
        for action in existing:
            try:
                params = json.loads(action.action_params) if action.action_params else {}
            except (TypeError, ValueError):
                continue
            if params.get("pool_name") == pool and params.get("trash_id") == trash_id:
                raise HTTPException(
                    status_code=409,
                    detail="Đã có một đề xuất xoá cho volume này đang chờ duyệt hoặc đã duyệt.",
                )

        incident = Incident(
            ceph_code=RBD_TRASH_REMOVE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất xoá vĩnh viễn volume trong trash {pool}/{trash_id} bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=RBD_TRASH_REMOVE_ACTION_ID,
            classification=gate.classify_action(RBD_TRASH_REMOVE_ACTION_ID).value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"Xoá vĩnh viễn volume {pool}/{trash_id} khỏi trash — dữ liệu không thể khôi phục "
                f"sau khi thực thi."
            ),
            target_nodes=json.dumps([mon_nodes[0]]),
            action_params=json.dumps(action_params),
            proposed_command=preview_command,
        )
        session.add(action)
        session.flush()

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()

    return RedirectResponse(url=f"/trash?pool={pool}", status_code=303)


@router.post("/volumes/{pool}/trash/purge-all", response_class=HTMLResponse)
async def purge_all_rbd_trash(request: Request, pool: str, user: str = Depends(require_login)):
    """Snapshot current trash IDs into one RISKY action; never purge inline."""
    _require_admin_privilege(user)
    _require_default_cluster_operation(request)
    pools = await asyncio.to_thread(_rbd_pools_for_request, request)
    allowed_pools = set(pools)
    if pool not in allowed_pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    try:
        entries = await asyncio.to_thread(ceph_client.query_rbd_trash, pool)
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không lấy được danh sách trash: {exc}")
    trash_ids = [str(entry["id"]) for entry in entries if _trash_retention(dict(entry))["purge_eligible"]]
    if not trash_ids:
        raise HTTPException(status_code=409, detail="Không có Trash item nào đã hết TTL để purge")
    mon_nodes = ceph_client.get_mon_nodes()
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="Chưa cấu hình CEPH_MON_NODES")
    action_params = {"pool_name": pool, "trash_ids": trash_ids}
    try:
        preview = executor_commands.get_command(
            RBD_TRASH_PURGE_ALL_ACTION_ID, mon_nodes[0], action_params
        )
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")
    with db.SessionLocal() as session:
        existing = session.query(Action).filter(
            Action.action_id == RBD_TRASH_PURGE_ALL_ACTION_ID,
            Action.status.in_(_IN_FLIGHT_ACTION_STATUSES),
        ).all()
        for row in existing:
            try:
                params = json.loads(row.action_params or "{}")
            except (TypeError, ValueError):
                continue
            if params.get("pool_name") == pool:
                raise HTTPException(status_code=409, detail="Pool này đã có đề xuất purge-all đang chờ")
        incident = Incident(
            ceph_code=RBD_TRASH_PURGE_ALL_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất purge-all {len(trash_ids)} Trash item trong pool {pool} bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=RBD_TRASH_PURGE_ALL_ACTION_ID,
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=f"Xoá vĩnh viễn {len(trash_ids)} Trash item trong pool {pool}; không thể hoàn tác",
            target_nodes=json.dumps([mon_nodes[0]]),
            action_params=json.dumps(action_params),
            proposed_command=preview,
        )
        session.add(action)
        session.flush()

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()

    return RedirectResponse(url=f"/trash?pool={pool}", status_code=303)
