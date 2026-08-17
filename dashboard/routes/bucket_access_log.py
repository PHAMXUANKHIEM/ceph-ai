"""Bucket Access Log page (equivalent to Ceph's native S3 Bucket Logging
for older Ceph versions without it — see watcher/rgw_access_log.py's own
docstring for the full reasoning). Route-only reads, same AD-3 posture as
dashboard/routes/nodes.py's rgw_log_api — no S3 credentials involved
anywhere in this feature, only SSH access to an already-configured RGW
node.

Also owns a dedicated "Cấu hình RGW" save form — the 2 fields this feature
actually needs (ceph_rgw_nodes/ceph_rgw_container_name) so an operator can
set up bucket logging entirely from this page, without a trip to the main
Settings page. These are the SAME underlying settings.py fields (and .env
vars) the Settings page's "Kết nối cụm Ceph" form edits — deliberately NOT
removed from there: shared/cluster_nodes.py::configured_nodes() (the SSH
SSRF whitelist Chat-with-AI/deploy/upgrade/patch/convert-cluster all read)
folds RGW nodes into the same shared node list those unrelated features
also depend on, so this is an ADDITIONAL, more convenient entry point for
these 2 fields, not the only one.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.cluster_scope import cluster_selection, selected_cluster
from dashboard.routes.auth import require_login
from dashboard.routes.settings import restart_watcher, restart_worker
from dashboard.templating import make_templates
from dashboard.vntime import to_utc_iso
from shared.cluster_nodes import configured_nodes as _configured_nodes, resolve_ssh_creds
from shared import db
from shared.models import Cluster
from shared.env_config import CLUSTER_ENV_NAMES, update_env_file_batch
from watcher.rgw_access_log import (
    RgwLogError,
    fetch_bucket_access_log,
    fetch_bucket_access_log_with,
    fetch_bucket_stats,
    fetch_bucket_stats_with,
    summarize_bucket_stats,
)

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


def _rgw_hosts(cluster) -> list[dict]:
    nodes = _configured_nodes() if cluster.is_default else _configured_nodes(cluster)
    return [node for node in nodes if "RGW" in node["roles"]]


def _context(
    user: str,
    cluster,
    clusters,
    *,
    bucket: str = "",
    config_error: str | None = None,
    config_success: str | None = None,
) -> dict:
    exec_mode = settings.ceph_exec_mode if cluster.is_default else cluster.ceph_exec_mode
    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "rgw_hosts": _rgw_hosts(cluster),
        "ceph_rgw_nodes": settings.ceph_rgw_nodes if cluster.is_default else cluster.ceph_rgw_nodes,
        "ceph_rgw_container_name": settings.ceph_rgw_container_name if cluster.is_default else cluster.ceph_rgw_container_name,
        "ceph_exec_mode": exec_mode,
        "clusters": clusters,
        "selected_cluster": cluster,
        "config_error": config_error,
        "config_success": config_success,
        "bucket": bucket,
    }


@router.get("/bucket-access-log", response_class=HTMLResponse)
async def index(
    request: Request,
    user: str = Depends(require_login),
    bucket: str = Query("", max_length=255),
):
    clusters, cluster = cluster_selection(request)
    return templates.TemplateResponse(
        request, "bucket_access_log.html", _context(user, cluster, clusters, bucket=bucket.strip())
    )


@router.post("/bucket-access-log/settings", response_class=HTMLResponse)
async def bucket_access_log_settings_submit(
    request: Request,
    user: str = Depends(require_login),
    ceph_rgw_nodes: str = Form(""),
    ceph_rgw_container_name: str = Form(""),
):
    """Same 2 fields, same env vars, same permission level (no admin
    requirement — matches dashboard/routes/settings.py::
    cluster_settings_submit, the field's other save path) as the main
    Settings page's cluster form. Skips that form's live MON-connectivity
    test on purpose — it tests `ceph health` against MON nodes, which says
    nothing about RGW node reachability; this page's own "Xem log" button
    is already the real connectivity check for THESE 2 fields (surfaces a
    clear RgwLogError -> 502 if the saved values don't work). Still
    restarts Watcher/Worker after saving (asyncio.to_thread, same as
    cluster_settings_submit) — both hold their OWN in-memory settings copy
    and depend on configured_nodes() including the current RGW node list.
    """
    clusters, cluster = cluster_selection(request)
    rgw_nodes = ceph_rgw_nodes.strip()
    rgw_container_name = ceph_rgw_container_name.strip()

    exec_mode = settings.ceph_exec_mode if cluster.is_default else cluster.ceph_exec_mode
    container_required = exec_mode not in ("none", "cephadm")
    if rgw_nodes and container_required and not rgw_container_name:
        return templates.TemplateResponse(
            request,
            "bucket_access_log.html",
            _context(
                user, cluster, clusters,
                config_error=(
                    f"Kiểu deploy hiện tại ({exec_mode}) cần tên container RGW."
                ),
            ),
        )

    try:
        if cluster.is_default:
            update_env_file_batch(
                {
                    CLUSTER_ENV_NAMES["ceph_rgw_nodes"]: rgw_nodes,
                    CLUSTER_ENV_NAMES["ceph_rgw_container_name"]: rgw_container_name,
                }
            )
            settings.ceph_rgw_nodes = rgw_nodes
            settings.ceph_rgw_container_name = rgw_container_name
        else:
            with db.SessionLocal() as session:
                target = session.get(Cluster, cluster.id)
                target.ceph_rgw_nodes = rgw_nodes
                target.ceph_rgw_container_name = rgw_container_name
                session.commit()
            cluster.ceph_rgw_nodes = rgw_nodes
            cluster.ceph_rgw_container_name = rgw_container_name
    except Exception:
        logger.exception("bucket_access_log_settings_submit: failed to persist config to .env")
        return templates.TemplateResponse(
            request,
            "bucket_access_log.html",
            _context(
                user, cluster, clusters, config_error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server"
            ),
        )

    watcher_restart = await asyncio.to_thread(restart_watcher)
    worker_restart = await asyncio.to_thread(restart_worker)
    warnings = []
    if not watcher_restart["restarted"]:
        warnings.append("Không tự khởi động lại được Watcher — khởi động lại thủ công.")
    if not worker_restart["restarted"]:
        warnings.append("Không tự khởi động lại được Worker — khởi động lại thủ công.")

    success = "Đã lưu cấu hình RGW."
    if warnings:
        success += " " + " ".join(warnings)

    return templates.TemplateResponse(
        request, "bucket_access_log.html", _context(user, cluster, clusters, config_success=success)
    )


@router.get("/api/bucket-access-log")
async def bucket_access_log_api(request: Request, host: str, bucket: str = "", user: str = Depends(require_login)):
    # Same SSRF-via-SSH whitelist posture as dashboard/routes/nodes.py's
    # rgw_log_api — `host` is attacker-reachable input, only an
    # already-configured RGW node may ever be queried.
    cluster = selected_cluster(request)
    rgw_hosts = {n["host"] for n in _rgw_hosts(cluster)}
    if host not in rgw_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách RGW đã cấu hình")
    try:
        if cluster.is_default:
            records = fetch_bucket_access_log(host, bucket)
        else:
            ssh_user, ssh_key_path, exec_mode, _container = resolve_ssh_creds(cluster)
            records = fetch_bucket_access_log_with(
                host, bucket, ssh_user, ssh_key_path, exec_mode, cluster.ceph_rgw_container_name
            )
    except RgwLogError as exc:
        logger.warning("bucket_access_log_api: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    # Bucket metadata is a SEPARATE radosgw-admin call from the log fetch
    # above — only attempted when a specific bucket is given (stats are
    # per-bucket, meaningless for the "all buckets" unfiltered view), and
    # its own failure degrades to bucket_stats=None rather than failing
    # the whole request: the access log itself is still useful on its own
    # even if radosgw-admin isn't reachable/installed where expected.
    bucket_stats = None
    if bucket:
        try:
            if cluster.is_default:
                raw_stats = fetch_bucket_stats(host, bucket)
            else:
                ssh_user, ssh_key_path, exec_mode, _container = resolve_ssh_creds(cluster)
                raw_stats = fetch_bucket_stats_with(
                    host, bucket, ssh_user, ssh_key_path, exec_mode, cluster.ceph_rgw_container_name
                )
            if raw_stats:
                bucket_stats = summarize_bucket_stats(raw_stats)
                bucket_stats["creation_time"] = (
                    to_utc_iso(bucket_stats["creation_time"]) if bucket_stats["creation_time"] else None
                )
        except RgwLogError as exc:
            logger.warning("bucket_access_log_api: bucket stats fetch failed: %s", exc)

    return {
        "host": host,
        "bucket": bucket,
        "bucket_stats": bucket_stats,
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
