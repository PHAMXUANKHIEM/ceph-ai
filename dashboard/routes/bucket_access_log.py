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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.cluster_scope import cluster_connection, cluster_selection, selected_cluster
from dashboard.routes.auth import require_login
from dashboard.routes.settings import restart_watcher, restart_worker
from dashboard.templating import make_templates
from dashboard.vntime import to_utc_iso
from shared.cluster_nodes import configured_nodes as _configured_nodes, resolve_ssh_creds
from shared import db
from shared.models import (
    BucketLoggingConfig, Cluster, ObjectStorageAuditEntry, RgwAccessAuditEvent,
)
from shared.ceph_releases import codename_for_version
from shared.env_config import CLUSTER_ENV_NAMES, update_env_file_batch
from watcher.rgw_access_log import (
    RgwLogError,
    fetch_bucket_access_log,
    fetch_bucket_access_log_with,
    fetch_bucket_stats,
    fetch_bucket_stats_with,
    summarize_bucket_stats,
)
from watcher import ceph_client
from watcher.ceph_client import CephQueryError

logger = logging.getLogger(__name__)
_VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

router = APIRouter()
templates = make_templates()


def _logging_payload(body: dict) -> dict:
    action = str(body.get("action") or "enable")
    if action not in {"enable", "disable"}:
        raise HTTPException(status_code=400, detail="Thao tác Bucket Logging không hợp lệ")
    source = str(body.get("source_bucket") or "").strip()
    target = str(body.get("target_bucket") or "").strip()
    owner = str(body.get("owner") or "").strip()
    endpoint = str(body.get("endpoint") or "").strip()
    prefix = str(body.get("prefix") or "logs/")
    if not source or len(source) > 255 or "/" in source:
        raise HTTPException(status_code=400, detail="Source bucket không hợp lệ")
    if action == "enable" and (not target or target == source or len(target) > 255 or "/" in target):
        raise HTTPException(status_code=400, detail="Target bucket phải hợp lệ và khác source bucket")
    if not owner or not endpoint.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Owner/endpoint không hợp lệ")
    if len(prefix) > 1024 or any(ord(char) < 32 for char in prefix):
        raise HTTPException(status_code=400, detail="Prefix không hợp lệ")
    return {"action": action, "source_bucket": source, "target_bucket": target,
            "owner": owner, "endpoint": endpoint, "prefix": prefix}


def _logging_targets(cluster, payload: dict) -> None:
    from dashboard.routes.object_storage import _detail
    source = _detail(cluster, payload["source_bucket"])
    if source.get("owner") != payload["owner"]:
        raise HTTPException(status_code=409, detail="Source bucket không thuộc owner đã chọn")
    if payload["action"] == "disable":
        return
    target = _detail(cluster, payload["target_bucket"])
    if target.get("owner") != payload["owner"]:
        raise HTTPException(status_code=409, detail="Source, target và owner phải cùng chủ sở hữu trong chế độ hiện tại")


def _apply_logging(cluster, payload: dict, mode: str) -> None:
    from dashboard.routes.object_storage import _with_owner_s3
    if mode == "native":
        def apply(client):
            status = {} if payload["action"] == "disable" else {"LoggingEnabled": {
                "TargetBucket": payload["target_bucket"], "TargetPrefix": payload["prefix"]}}
            client.put_bucket_logging(Bucket=payload["source_bucket"], BucketLoggingStatus=status)
        _with_owner_s3(cluster, payload, apply)
    with db.SessionLocal() as session:
        row = session.query(BucketLoggingConfig).filter_by(
            cluster_id=cluster.id, source_bucket=payload["source_bucket"]).one_or_none()
        if payload["action"] == "disable":
            if row:
                row.enabled = False
                row.updated_at = datetime.utcnow()
            session.commit()
            return
        if row is None:
            row = BucketLoggingConfig(cluster_id=cluster.id, source_bucket=payload["source_bucket"])
            session.add(row)
        row.target_bucket = payload["target_bucket"]
        row.prefix = payload["prefix"]
        row.owner = payload["owner"]
        row.endpoint = payload["endpoint"]
        row.mode = mode
        row.enabled = True
        row.last_error = None
        row.updated_at = datetime.utcnow()
        session.commit()


def _logging_capability(cluster) -> dict:
    """Detect native S3 Bucket Logging from the live Ceph release.

    Native bucket-to-bucket logging is new in Tentacle (major 20).  The
    Beast HTTP access log parsed by this page is a separate fallback and must
    never be presented as native S3 Bucket Logging.
    """
    try:
        if cluster.is_default:
            versions = ceph_client.summarize_cluster_versions()
        else:
            _host, payload = ceph_client.run_ceph_json_command_with(
                *cluster_connection(cluster), "ceph versions"
            )
            versions = ceph_client.summarize_versions_payload(payload)
    except CephQueryError as exc:
        logger.warning("bucket logging capability check failed: %s", exc)
        return {"known": False, "native_supported": False, "fallback_supported": False,
                "reason": "Không lấy được phiên bản Ceph của cluster đang chọn."}
    version = versions.get("current_version")
    if not version:
        return {"known": False, "native_supported": False, "fallback_supported": False,
                "reason": "Cluster đang chạy lẫn hoặc không xác định được phiên bản Ceph."}
    try:
        major = int(str(version).split(".", 1)[0])
    except ValueError:
        return {"known": False, "native_supported": False, "fallback_supported": False,
                "reason": f"Không đọc được major version từ Ceph {version}."}
    native = major >= 20
    fallback = major >= 14
    return {
        "known": True, "ceph_version": version, "ceph_release": codename_for_version(version),
        "native_supported": native, "native_min_ceph_major": 20,
        "fallback_supported": fallback, "fallback_min_ceph_major": 14,
        "mode": "native_available" if native else "beast_access_log" if fallback else "unsupported",
        "reason": None if native else (
            "Native S3 Bucket Logging cần Ceph Tentacle 20 trở lên. "
            + ("Trang này chỉ đọc HTTP access log của RGW Beast; log không được ghi sang bucket đích."
               if fallback else "Ceph hiện tại cũng chưa đạt mốc Nautilus 14 cho fallback Beast đã kiểm chứng.")
        ),
        "documentation": "https://docs.ceph.com/en/latest/radosgw/bucket_logging/",
    }


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
    logging_capability: dict | None = None,
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
        "logging_capability": logging_capability,
    }


@router.get("/bucket-access-log", response_class=HTMLResponse)
async def index(
    request: Request,
    user: str = Depends(require_login),
    bucket: str = Query("", max_length=255),
):
    clusters, cluster = cluster_selection(request)
    capability = await asyncio.to_thread(_logging_capability, cluster)
    return templates.TemplateResponse(
        request, "bucket_access_log.html", _context(
            user, cluster, clusters, bucket=bucket.strip(), logging_capability=capability
        )
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
    capability = await asyncio.to_thread(_logging_capability, cluster)
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
        "logging_capability": capability,
        "records": [
            {
                "remote_addr": r["remote_addr"],
                "requester": r.get("requester"),
                "user_agent": r.get("user_agent"),
                "timestamp": to_utc_iso(r["timestamp"]) if r["timestamp"] else None,
                "timestamp_raw": r["timestamp_raw"],
                "method": r["method"],
                "path": r["path"],
                "bucket": r["bucket"],
                "object": r["object"],
                "action": r["action"],
                "status": r["status"],
                "bytes_sent": r["bytes_sent"],
                "latency_ms": r.get("latency_ms"),
            }
            for r in records
        ],
    }


@router.get("/api/bucket-access-history")
async def bucket_access_history_api(
    request: Request,
    ip: str = Query("", max_length=255),
    requester: str = Query("", max_length=255),
    bucket: str = Query("", max_length=255),
    method: str = Query("", max_length=16),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    user: str = Depends(require_login),
):
    """Persistent RGW audit history, unlike the bounded live daemon tail."""
    cluster = selected_cluster(request)
    method = method.strip().upper()
    if method and method not in {"GET", "PUT", "POST", "DELETE", "HEAD", "PATCH", "OPTIONS"}:
        raise HTTPException(status_code=400, detail="HTTP method không hợp lệ")
    with db.SessionLocal() as session:
        query = session.query(RgwAccessAuditEvent).filter_by(cluster_id=cluster.id)
        if ip.strip():
            query = query.filter(RgwAccessAuditEvent.remote_addr == ip.strip())
        if requester.strip():
            query = query.filter(RgwAccessAuditEvent.requester.ilike(f"%{requester.strip()}%"))
        if bucket.strip():
            query = query.filter(RgwAccessAuditEvent.bucket.ilike(f"%{bucket.strip()}%"))
        if method:
            query = query.filter(RgwAccessAuditEvent.method == method)
        if date_from:
            source = date_from if date_from.tzinfo else date_from.replace(tzinfo=_VN_TZ)
            query = query.filter(RgwAccessAuditEvent.event_at >= source.astimezone(timezone.utc).replace(tzinfo=None))
        if date_to:
            source = date_to if date_to.tzinfo else date_to.replace(tzinfo=_VN_TZ)
            query = query.filter(RgwAccessAuditEvent.event_at <= source.astimezone(timezone.utc).replace(tzinfo=None))
        total = query.count()
        rows = query.order_by(RgwAccessAuditEvent.event_at.desc()).offset(
            (page - 1) * page_size
        ).limit(page_size).all()
        items = [{
            "id": row.id, "request_id": row.transaction_id,
            "timestamp": to_utc_iso(row.event_at), "ip": row.remote_addr,
            "requester": row.requester, "method": row.method, "action": row.action,
            "bucket": row.bucket, "object": row.object_key, "status": row.http_status,
            "size": row.bytes_sent, "encryption": row.encryption, "rgw_host": row.rgw_host,
        } for row in rows]
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size)}


@router.post("/api/bucket-logging/preview")
async def bucket_logging_preview(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được cấu hình Bucket Logging")
    payload = _logging_payload(await request.json())
    cluster = selected_cluster(request)
    capability = await asyncio.to_thread(_logging_capability, cluster)
    if not capability.get("known") or not capability.get("fallback_supported"):
        raise HTTPException(status_code=409, detail=capability.get("reason") or "Version không hỗ trợ")
    await asyncio.to_thread(_logging_targets, cluster, payload)
    mode = "native" if capability["native_supported"] else "compatibility"
    return {"action": payload["action"], "source_bucket": payload["source_bucket"],
            "target_bucket": payload["target_bucket"], "prefix": payload["prefix"],
            "mode": mode, "ceph_version": capability["ceph_version"],
            "confirmation_required": payload["source_bucket"], "risk": "medium",
            "warning": None if mode == "native" else
                "Chế độ tương thích gom Beast access log định kỳ; không có đầy đủ bảo đảm native Tentacle."}


@router.post("/api/bucket-logging/execute")
async def bucket_logging_execute(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được cấu hình Bucket Logging")
    body = await request.json()
    payload = _logging_payload(body)
    if str(body.get("confirmation") or "") != payload["source_bucket"]:
        raise HTTPException(status_code=400, detail="Nhập chính xác source bucket để xác nhận")
    cluster = selected_cluster(request)
    capability = await asyncio.to_thread(_logging_capability, cluster)
    if not capability.get("known") or not capability.get("fallback_supported"):
        raise HTTPException(status_code=409, detail=capability.get("reason") or "Version không hỗ trợ")
    await asyncio.to_thread(_logging_targets, cluster, payload)
    mode = "native" if capability["native_supported"] else "compatibility"
    preview = (f"{payload['action']} mode={mode} source={payload['source_bucket']} "
               f"target={payload['target_bucket']} prefix={payload['prefix']}")
    with db.SessionLocal() as session:
        audit = ObjectStorageAuditEntry(cluster_id=cluster.id, actor=user,
            action=f"bucket_logging_{payload['action']}", target_type="bucket",
            target_id=payload["source_bucket"], preview=preview, result="pending")
        session.add(audit)
        session.commit()
        audit_id = audit.id
    try:
        await asyncio.to_thread(_apply_logging, cluster, payload, mode)
    except Exception as exc:
        with db.SessionLocal() as session:
            row = session.get(ObjectStorageAuditEntry, audit_id)
            row.result = "failed"; row.error_message = "Không áp dụng được Bucket Logging"; row.completed_at = datetime.utcnow()
            session.commit()
        raise HTTPException(status_code=502, detail="Không áp dụng được Bucket Logging") from exc
    with db.SessionLocal() as session:
        row = session.get(ObjectStorageAuditEntry, audit_id)
        row.result = "succeeded"; row.completed_at = datetime.utcnow(); session.commit()
    return {"ok": True, "mode": mode, "request_id": audit_id}
