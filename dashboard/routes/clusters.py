import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.settings import restart_watcher, restart_worker
from dashboard.templating import make_templates
from shared import db
from shared.clusters import ensure_default_cluster
from shared.models import Action, AuditEntry, BackupAnomaly, BackupJob, Cluster, Incident, WatcherHeartbeat
from watcher.ceph_client import VALID_EXEC_MODES, CephQueryError, query_cluster_health_with

VALID_BACKUP_TRANSPORTS = ("ssh", "s3", "")

router = APIRouter()
templates = make_templates()
logger = logging.getLogger(__name__)


def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )


def _parse_node_list(raw: str) -> list[str]:
    return [h.strip() for h in raw.split(",") if h.strip()]


def _list_clusters() -> list[Cluster]:
    """Default cluster first (it's not editable here, see Cluster's
    docstring), then additional observed clusters newest first."""
    with db.SessionLocal() as session:
        ensure_default_cluster(session)
        rows = session.query(Cluster).order_by(Cluster.is_default.desc(), Cluster.created_at.desc()).all()
        session.expunge_all()
        return rows


def _clusters_context(
    user: str,
    *,
    cluster_create_error: str | None = None,
    cluster_create_success: str | None = None,
    cluster_toggle_error: str | None = None,
    cluster_toggle_success: str | None = None,
    cluster_delete_error: str | None = None,
    cluster_delete_success: str | None = None,
    cluster_form_values: dict | None = None,
    backup_config_error: str | None = None,
    backup_config_success: str | None = None,
    backup_config_cluster_id: str | None = None,
    backup_config_form_values: dict | None = None,
) -> dict:
    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": _list_clusters(),
        "cluster_create_error": cluster_create_error,
        "cluster_create_success": cluster_create_success,
        "cluster_toggle_error": cluster_toggle_error,
        "cluster_toggle_success": cluster_toggle_success,
        "cluster_delete_error": cluster_delete_error,
        "cluster_delete_success": cluster_delete_success,
        "cluster_form_values": cluster_form_values or {},
        # Multi-tenant remediation Phase 3 — which cluster's backup-config
        # sub-form (one per additional cluster row) an error/success/
        # redisplay belongs to; every OTHER cluster's sub-form just shows
        # its own current DB values, untouched.
        "backup_config_error": backup_config_error,
        "backup_config_success": backup_config_success,
        "backup_config_cluster_id": backup_config_cluster_id,
        "backup_config_form_values": backup_config_form_values or {},
    }


@router.get("/clusters", response_class=HTMLResponse)
async def clusters_page(request: Request, user: str = Depends(require_login)):
    """Multi-cluster observability (Phase 1) — manage ADDITIONAL clusters
    Watcher polls for health/incidents alongside the default one. The
    default cluster itself stays governed by the existing "Kết nối cụm
    Ceph" section on /settings (`.env`-backed) — it's shown here read-only
    for context, not editable. Admin-only, same posture as /settings'
    cluster-connection form (these fields carry SSH credentials)."""
    _require_admin_privilege(user)
    return templates.TemplateResponse(request, "clusters.html", _clusters_context(user))


@router.post("/clusters/create", response_class=HTMLResponse)
async def create_cluster(
    request: Request,
    user: str = Depends(require_login),
    name: str = Form(""),
    ceph_mon_nodes: str = Form(""),
    ceph_mon_hostnames: str = Form(""),
    ceph_mgr_nodes: str = Form(""),
    ceph_osd_nodes: str = Form(""),
    ceph_rgw_nodes: str = Form(""),
    ceph_container_name: str = Form(""),
    ceph_osd_container_name: str = Form(""),
    ceph_rgw_container_name: str = Form(""),
    ssh_user: str = Form(""),
    ssh_key_path: str = Form(""),
    ceph_exec_mode: str = Form("docker"),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    telegram_enabled: str = Form(""),
    backup_enabled: str = Form(""),
    backup_tracked_images: str = Form(""),
    backup_full_refresh_days: str = Form(""),
    backup_transport: str = Form(""),
    backup_ssh_host: str = Form(""),
    backup_ssh_user: str = Form(""),
    backup_ssh_key_path: str = Form(""),
    backup_ssh_landing_dir: str = Form(""),
    backup_s3_endpoint: str = Form(""),
    backup_s3_access_key: str = Form(""),
    backup_s3_secret_key: str = Form(""),
    backup_s3_bucket: str = Form(""),
    backup_immutable_enabled: str = Form(""),
    backup_immutable_lock_days: str = Form(""),
):
    """Adds an additional observed cluster — tests the connection with the
    submitted values BEFORE saving (same AC as /settings' cluster form,
    reusing the same watcher/ceph_client.query_cluster_health_with helper),
    so a typo'd MON node/SSH key never silently gets saved as if it worked.

    2026-08-10 (multi-tenant remediation Phase 1): MGR/OSD/RGW nodes + RGW
    container name are all OPTIONAL, same posture as their `.env` equivalents
    (config/settings.py's own fields) — only MON nodes are required to add a
    cluster at all (the connection test itself only ever needs MON). Left
    blank, `shared/cluster_nodes.py::configured_nodes(cluster)` for this
    cluster just returns MON-only, same as an unconfigured `.env` today.

    2026-08-10 (multi-tenant remediation Phase 2): telegram_bot_token/
    chat_id are also OPTIONAL and NOT part of the pre-save connection test
    above (same "Gửi thử là hành động riêng" posture the 3 global channels'
    own /telegram-alerts page already has) — left blank, this cluster's
    alerts/RISKY approvals fall back to the 3 global channels
    (dashboard/telegram_approval_bot.py::channels_for_incident).

    2026-08-11 (multi-tenant remediation Phase 3): backup_* fields are also
    OPTIONAL and NOT part of the pre-save connection test — this cluster's
    own RBD/SSH-or-S3 backup target is a SEPARATE credential path from the
    source-cluster SSH creds above (PRD FR-4), reachability of a backup
    destination is a different check than reachability of a Ceph MON, and
    is deliberately out of this phase's scope (same "gửi thử là hành động
    riêng" reasoning Phase 2 used). Left blank, this cluster simply has no
    backup pipeline (`worker/backup/scheduler.py::build_scheduler()` never
    registers a job for it)."""
    _require_admin_privilege(user)

    submitted = {
        "name": name.strip(),
        "ceph_mon_nodes": ceph_mon_nodes.strip(),
        "ceph_mon_hostnames": ceph_mon_hostnames.strip(),
        "ceph_mgr_nodes": ceph_mgr_nodes.strip(),
        "ceph_osd_nodes": ceph_osd_nodes.strip(),
        "ceph_rgw_nodes": ceph_rgw_nodes.strip(),
        "ceph_container_name": ceph_container_name.strip(),
        "ceph_osd_container_name": ceph_osd_container_name.strip(),
        "ceph_rgw_container_name": ceph_rgw_container_name.strip(),
        "ssh_user": ssh_user.strip(),
        "ssh_key_path": ssh_key_path.strip(),
        "ceph_exec_mode": ceph_exec_mode.strip() or "docker",
        "telegram_bot_token": telegram_bot_token.strip(),
        "telegram_chat_id": telegram_chat_id.strip(),
        "telegram_enabled": telegram_enabled.strip().lower() in ("true", "on", "1"),
        "backup_enabled": backup_enabled.strip().lower() in ("true", "on", "1"),
        "backup_tracked_images": backup_tracked_images.strip(),
        "backup_full_refresh_days": backup_full_refresh_days.strip(),
        "backup_transport": backup_transport.strip(),
        "backup_ssh_host": backup_ssh_host.strip(),
        "backup_ssh_user": backup_ssh_user.strip(),
        "backup_ssh_key_path": backup_ssh_key_path.strip(),
        "backup_ssh_landing_dir": backup_ssh_landing_dir.strip(),
        "backup_s3_endpoint": backup_s3_endpoint.strip(),
        "backup_s3_access_key": backup_s3_access_key.strip(),
        "backup_s3_secret_key": backup_s3_secret_key.strip(),
        "backup_s3_bucket": backup_s3_bucket.strip(),
        "backup_immutable_enabled": backup_immutable_enabled.strip().lower() in ("true", "on", "1"),
        "backup_immutable_lock_days": backup_immutable_lock_days.strip(),
    }
    mon_nodes_list = _parse_node_list(submitted["ceph_mon_nodes"])

    if submitted["ceph_exec_mode"] not in VALID_EXEC_MODES:
        return templates.TemplateResponse(
            request,
            "clusters.html",
            _clusters_context(
                user,
                cluster_create_error=f"Kiểu deploy không hợp lệ: {submitted['ceph_exec_mode']!r}",
                cluster_form_values=submitted,
            ),
        )

    container_required = submitted["ceph_exec_mode"] not in ("none", "cephadm")
    if (
        not submitted["name"]
        or not mon_nodes_list
        or (container_required and not submitted["ceph_container_name"])
        or not submitted["ssh_user"]
        or not submitted["ssh_key_path"]
    ):
        message = (
            "Vui lòng điền đủ Tên cụm, MON nodes, MON container, SSH user, SSH key path."
            if container_required
            else "Vui lòng điền đủ Tên cụm, MON nodes, SSH user, SSH key path."
        )
        return templates.TemplateResponse(
            request,
            "clusters.html",
            _clusters_context(user, cluster_create_error=message, cluster_form_values=submitted),
        )

    if submitted["backup_transport"] not in VALID_BACKUP_TRANSPORTS:
        return templates.TemplateResponse(
            request,
            "clusters.html",
            _clusters_context(
                user,
                cluster_create_error=f"Nơi lưu backup không hợp lệ: {submitted['backup_transport']!r}",
                cluster_form_values=submitted,
            ),
        )
    if submitted["backup_enabled"] and not submitted["backup_transport"]:
        return templates.TemplateResponse(
            request,
            "clusters.html",
            _clusters_context(
                user,
                cluster_create_error="Bật backup cần chọn nơi lưu (SSH hoặc S3).",
                cluster_form_values=submitted,
            ),
        )

    try:
        # update_sticky_fallback=False — this is an ADDITIONAL (non-default)
        # cluster's candidate config; a successful test here must never
        # overwrite the sticky MON node the DEFAULT cluster's log collection
        # depends on (watcher/ceph_client.py::query_cluster_health_with's
        # own docstring).
        await asyncio.to_thread(
            query_cluster_health_with,
            mon_nodes_list,
            submitted["ceph_container_name"],
            submitted["ssh_user"],
            submitted["ssh_key_path"],
            submitted["ceph_exec_mode"],
            update_sticky_fallback=False,
        )
    except CephQueryError as exc:
        logger.warning("create_cluster: connection test failed: %s", exc)
        return templates.TemplateResponse(
            request,
            "clusters.html",
            _clusters_context(
                user, cluster_create_error=f"Không kết nối được tới cụm: {exc}", cluster_form_values=submitted
            ),
        )

    with db.SessionLocal() as session:
        session.add(
            Cluster(
                name=submitted["name"],
                ceph_mon_nodes=submitted["ceph_mon_nodes"],
                ceph_mon_hostnames=submitted["ceph_mon_hostnames"],
                ceph_mgr_nodes=submitted["ceph_mgr_nodes"],
                ceph_osd_nodes=submitted["ceph_osd_nodes"],
                ceph_rgw_nodes=submitted["ceph_rgw_nodes"],
                ceph_container_name=submitted["ceph_container_name"],
                ceph_osd_container_name=submitted["ceph_osd_container_name"],
                ceph_rgw_container_name=submitted["ceph_rgw_container_name"],
                ssh_user=submitted["ssh_user"],
                ssh_key_path=submitted["ssh_key_path"],
                ceph_exec_mode=submitted["ceph_exec_mode"],
                telegram_bot_token=submitted["telegram_bot_token"],
                telegram_chat_id=submitted["telegram_chat_id"],
                telegram_enabled=submitted["telegram_enabled"],
                backup_enabled=submitted["backup_enabled"],
                backup_tracked_images=submitted["backup_tracked_images"],
                backup_full_refresh_days=(
                    int(submitted["backup_full_refresh_days"]) if submitted["backup_full_refresh_days"].isdigit() else None
                ),
                backup_transport=submitted["backup_transport"],
                backup_ssh_host=submitted["backup_ssh_host"],
                backup_ssh_user=submitted["backup_ssh_user"],
                backup_ssh_key_path=submitted["backup_ssh_key_path"],
                backup_ssh_landing_dir=submitted["backup_ssh_landing_dir"],
                backup_s3_endpoint=submitted["backup_s3_endpoint"],
                backup_s3_access_key=submitted["backup_s3_access_key"],
                backup_s3_secret_key=submitted["backup_s3_secret_key"],
                backup_s3_bucket=submitted["backup_s3_bucket"],
                backup_immutable_enabled=submitted["backup_immutable_enabled"],
                backup_immutable_lock_days=(
                    int(submitted["backup_immutable_lock_days"]) if submitted["backup_immutable_lock_days"].isdigit() else 7
                ),
                is_default=False,
                is_active=True,
            )
        )
        session.commit()

    # `run_all_clusters()` (watcher/main.py) only ever enumerates the
    # "Đang hoạt động" cluster list ONCE, at Watcher process startup — one
    # background poll thread per cluster, started then and never again.
    # Without restarting Watcher here, a newly-added cluster sits in the DB
    # with no poll thread ever started for it: no heartbeat, no Incident,
    # ever — the Dashboard's cluster selector would show it as if it were
    # never actually being watched. Same "save = restart the process that
    # reads this config" posture every other config page in this app already
    # has (Alert Telegram, Settings' own "Kết nối cụm Ceph"). Worker's own
    # scheduler (worker/backup/scheduler.py::build_scheduler()) has the
    # exact same "enumerates clusters once, at its own startup" shape
    # (multi-tenant remediation Phase 3) — restarted too so a cluster
    # created with backup_enabled already set gets its jobs registered
    # without a second, separate save.
    watcher_result, worker_result = await asyncio.gather(
        asyncio.to_thread(restart_watcher), asyncio.to_thread(restart_worker)
    )
    restart_failures = [
        name for name, result in (("Watcher", watcher_result), ("Worker", worker_result))
        if not result.get("restarted")
    ]
    if restart_failures:
        return templates.TemplateResponse(
            request, "clusters.html", _clusters_context(
                user,
                cluster_create_error=(
                    f"Đã lưu cụm {submitted['name']!r}, nhưng không restart được "
                    f"{', '.join(restart_failures)}. Cần restart thủ công để cấu hình có hiệu lực."
                ),
            ),
        )

    return templates.TemplateResponse(
        request,
        "clusters.html",
        _clusters_context(
            user,
            cluster_create_success=f"Đã thêm cụm {submitted['name']!r} — Watcher/Worker đã khởi động lại để bắt đầu giám sát/backup ngay.",
        ),
    )


@router.post("/clusters/{cluster_id}/toggle-active", response_class=HTMLResponse)
async def toggle_cluster_active(request: Request, cluster_id: str, user: str = Depends(require_login)):
    """Soft-disable/re-enable — never a hard delete (an Incident/
    WatcherHeartbeat row may already reference this cluster_id). The
    default cluster can never be a target here — deactivating it would stop
    Watcher's PRIMARY loop, including every secondary monitor and Worker's
    remediation, none of which this page controls; that stays a config
    change on /settings' own "Kết nối cụm Ceph" section instead. Restarts
    Watcher on success (see create_cluster()'s own comment for why)."""
    _require_admin_privilege(user)

    with db.SessionLocal() as session:
        target = session.get(Cluster, cluster_id)
        if target is None:
            return templates.TemplateResponse(
                request, "clusters.html", _clusters_context(user, cluster_toggle_error="Không tìm thấy cụm.")
            )
        if target.is_default:
            return templates.TemplateResponse(
                request,
                "clusters.html",
                _clusters_context(
                    user,
                    cluster_toggle_error=(
                        "Không thể vô hiệu hoá cụm mặc định ở đây — cụm này được quản lý qua "
                        "mục \"Kết nối cụm Ceph\" ở trang Cài đặt."
                    ),
                ),
            )
        target.is_active = not target.is_active
        session.commit()

    # Same reasoning as create_cluster() above — Watcher only enumerates
    # "Đang hoạt động" clusters once at startup, so flipping is_active here
    # has no real effect (poll thread doesn't start/stop) until Watcher
    # restarts.
    watcher_result, worker_result = await asyncio.gather(
        asyncio.to_thread(restart_watcher), asyncio.to_thread(restart_worker)
    )
    failed = [name for name, result in (("Watcher", watcher_result), ("Worker", worker_result))
              if not result.get("restarted")]
    if failed:
        return templates.TemplateResponse(
            request, "clusters.html", _clusters_context(
                user, cluster_toggle_error=(
                    f"Đã đổi trạng thái cụm nhưng không restart được {', '.join(failed)}; "
                    "cần restart thủ công để scheduler đồng bộ."
                )
            )
        )
    return templates.TemplateResponse(
        request, "clusters.html", _clusters_context(
            user, cluster_toggle_success="Đã đổi trạng thái cụm và đồng bộ Watcher/Worker."
        )
    )


@router.post("/clusters/{cluster_id}/connection", response_class=HTMLResponse)
async def update_cluster_connection(
    request: Request, cluster_id: str, user: str = Depends(require_login),
    name: str = Form(""), ceph_mon_nodes: str = Form(""),
    ceph_mon_hostnames: str = Form(""), ceph_mgr_nodes: str = Form(""),
    ceph_osd_nodes: str = Form(""), ceph_rgw_nodes: str = Form(""),
    ceph_container_name: str = Form(""), ceph_osd_container_name: str = Form(""),
    ceph_rgw_container_name: str = Form(""), ssh_user: str = Form(""),
    ssh_key_path: str = Form(""), ceph_exec_mode: str = Form("docker"),
):
    """Test then update an additional cluster's connection atomically."""
    _require_admin_privilege(user)
    values = {key: value.strip() for key, value in {
        "name": name, "ceph_mon_nodes": ceph_mon_nodes,
        "ceph_mon_hostnames": ceph_mon_hostnames, "ceph_mgr_nodes": ceph_mgr_nodes,
        "ceph_osd_nodes": ceph_osd_nodes, "ceph_rgw_nodes": ceph_rgw_nodes,
        "ceph_container_name": ceph_container_name,
        "ceph_osd_container_name": ceph_osd_container_name,
        "ceph_rgw_container_name": ceph_rgw_container_name,
        "ssh_user": ssh_user, "ssh_key_path": ssh_key_path,
        "ceph_exec_mode": ceph_exec_mode,
    }.items()}
    nodes = _parse_node_list(values["ceph_mon_nodes"])
    container_required = values["ceph_exec_mode"] not in ("none", "cephadm")
    if values["ceph_exec_mode"] not in VALID_EXEC_MODES or not values["name"] or not nodes \
            or not values["ssh_user"] or not values["ssh_key_path"] \
            or (container_required and not values["ceph_container_name"]):
        return templates.TemplateResponse(request, "clusters.html", _clusters_context(
            user, cluster_toggle_error="Thông tin kết nối cluster chưa đầy đủ hoặc exec mode không hợp lệ."
        ))
    with db.SessionLocal() as session:
        target = session.get(Cluster, cluster_id)
        if target is None or target.is_default:
            return templates.TemplateResponse(request, "clusters.html", _clusters_context(
                user, cluster_toggle_error="Không tìm thấy cluster phụ cần sửa."
            ))
    try:
        await asyncio.to_thread(
            query_cluster_health_with, nodes, values["ceph_container_name"],
            values["ssh_user"], values["ssh_key_path"], values["ceph_exec_mode"],
            update_sticky_fallback=False,
        )
    except CephQueryError as exc:
        return templates.TemplateResponse(request, "clusters.html", _clusters_context(
            user, cluster_toggle_error=f"Không lưu vì test kết nối thất bại: {exc}"
        ))
    with db.SessionLocal() as session:
        target = session.get(Cluster, cluster_id)
        for field, value in values.items():
            setattr(target, field, value)
        session.commit()
    watcher_result, worker_result = await asyncio.gather(
        asyncio.to_thread(restart_watcher), asyncio.to_thread(restart_worker)
    )
    failed = [label for label, result in (("Watcher", watcher_result), ("Worker", worker_result))
              if not result.get("restarted")]
    message = "Đã test và cập nhật kết nối cluster."
    if failed:
        return templates.TemplateResponse(request, "clusters.html", _clusters_context(
            user, cluster_toggle_error=message + f" Không restart được {', '.join(failed)}."
        ))
    return templates.TemplateResponse(request, "clusters.html", _clusters_context(
        user, cluster_toggle_success=message + " Watcher/Worker đã được đồng bộ."
    ))


def _purge_cluster_data(session, cluster_id: str) -> dict[str, int]:
    """Hard-deletes every DB row scoped to exactly this ONE additional
    (non-default) cluster — called by delete_cluster() below, inside the
    SAME session/transaction as that Cluster row's own deletion so a crash
    partway through can never leave orphaned child rows behind.

    Only 3 tables carry a `cluster_id`/FK chain back to `clusters.id`
    (Incident, WatcherHeartbeat, BackupJob — see shared/models.py; every
    OTHER table in this app is still default-cluster-only, per Cluster's
    own docstring: patch/upgrade/RestoreDrill/digest/CRUSH-monitor/
    volume-monitor/node-diagnostics never run for an
    additional cluster). So this is NOT a generic "cascade delete
    anything that might reference this row" helper — it is exactly these
    3 tables' rows, deleted in FK-dependency order (children before
    parents), the same "manually break/delete the FK before the parent"
    idiom dashboard/routes/maintenance.py::purge_old_records already uses
    for the identical Incident -> Action -> AuditEntry chain (there is no
    ORM/DB cascade configured anywhere in this codebase).

    Deliberately does NOT touch ChatMessage.proposed_incident_id (a
    nullable FK to Incident) the way purge_old_records does — that purge
    can hit ANY Incident, chat-originated ones included, so it must null
    the reference first. Chat (dashboard/routes/chat.py) has no cluster
    picker and only ever creates Incidents with cluster_id=None (the
    default cluster) — no ChatMessage row can ever reference a
    non-default cluster's Incident in the first place, so there is
    nothing to break here.
    """
    incident_ids = [
        row[0] for row in session.query(Incident.id).filter(Incident.cluster_id == cluster_id).all()
    ]
    audit_deleted = action_deleted = incident_deleted = 0
    if incident_ids:
        audit_deleted = (
            session.query(AuditEntry)
            .filter(AuditEntry.incident_id.in_(incident_ids))
            .delete(synchronize_session=False)
        )
        action_deleted = (
            session.query(Action)
            .filter(Action.incident_id.in_(incident_ids))
            .delete(synchronize_session=False)
        )
        incident_deleted = (
            session.query(Incident)
            .filter(Incident.id.in_(incident_ids))
            .delete(synchronize_session=False)
        )

    backup_job_ids = [
        row[0] for row in session.query(BackupJob.id).filter(BackupJob.cluster_id == cluster_id).all()
    ]
    backup_anomaly_deleted = backup_job_deleted = 0
    if backup_job_ids:
        backup_anomaly_deleted = (
            session.query(BackupAnomaly)
            .filter(BackupAnomaly.backup_job_id.in_(backup_job_ids))
            .delete(synchronize_session=False)
        )
        # BackupJob.base_job_id is a SELF-FK (an incremental row points at
        # the full export its export-diff chain is based on) — deleting a
        # whole cluster's chain via one bulk DELETE can otherwise trip that
        # constraint depending on row-processing order, so break every
        # self-reference within this cluster's own rows first (same "null
        # the FK before deleting" idiom as ChatMessage above, just
        # self-referencing instead of cross-table).
        session.query(BackupJob).filter(BackupJob.id.in_(backup_job_ids)).update(
            {"base_job_id": None}, synchronize_session=False
        )
        backup_job_deleted = (
            session.query(BackupJob).filter(BackupJob.id.in_(backup_job_ids)).delete(synchronize_session=False)
        )

    heartbeat_deleted = (
        session.query(WatcherHeartbeat)
        .filter(WatcherHeartbeat.cluster_id == cluster_id)
        .delete(synchronize_session=False)
    )

    return {
        "incidents": incident_deleted,
        "actions": action_deleted,
        "audit_entries": audit_deleted,
        "backup_jobs": backup_job_deleted,
        "backup_anomalies": backup_anomaly_deleted,
        "heartbeats": heartbeat_deleted,
    }


@router.post("/clusters/{cluster_id}/delete", response_class=HTMLResponse)
async def delete_cluster(request: Request, cluster_id: str, user: str = Depends(require_login)):
    """Hard delete — unlike toggle_cluster_active's soft-disable above,
    this PERMANENTLY removes the Cluster row and every Incident/Action/
    AuditEntry/BackupJob/BackupAnomaly/WatcherHeartbeat row scoped to it
    (see _purge_cluster_data's own docstring for exactly which tables,
    and why only those — nothing belonging to any OTHER cluster, default
    included, is ever touched). The default cluster can never be a
    target here, same guard/reasoning as toggle_cluster_active above."""
    _require_admin_privilege(user)

    with db.SessionLocal() as session:
        target = session.get(Cluster, cluster_id)
        if target is None:
            return templates.TemplateResponse(
                request, "clusters.html", _clusters_context(user, cluster_delete_error="Không tìm thấy cụm.")
            )
        if target.is_default:
            return templates.TemplateResponse(
                request,
                "clusters.html",
                _clusters_context(user, cluster_delete_error="Không thể xoá cụm mặc định."),
            )
        cluster_name = target.name
        counts = _purge_cluster_data(session, cluster_id)
        session.delete(target)
        session.commit()

    # Same "config only takes effect after a restart" reasoning as
    # create_cluster()/toggle_cluster_active() above — Watcher's poll
    # thread and Worker's backup-scheduler jobs for this cluster_id both
    # only ever get (de)registered once, at process startup. Skipping this
    # would leave a deleted cluster's Worker backup job still firing —
    # worker/backup/cluster_scope.py::get_cluster() re-fetches by id and
    # returns None once the row is gone, and a None cluster means "the
    # DEFAULT cluster" everywhere else in that module — so a stale job
    # would silently start backing up the DEFAULT cluster's images under
    # the deleted cluster's old schedule. A real data-safety bug, not just
    # a cosmetic stale-UI one, so both restart unconditionally on success.
    watcher_result, worker_result = await asyncio.gather(
        asyncio.to_thread(restart_watcher), asyncio.to_thread(restart_worker)
    )
    failed = [name for name, result in (("Watcher", watcher_result), ("Worker", worker_result))
              if not result.get("restarted")]

    return templates.TemplateResponse(
        request,
        "clusters.html",
        _clusters_context(
            user,
            cluster_delete_success=(
                f"Đã xoá cụm {cluster_name!r} và toàn bộ dữ liệu riêng của cụm này: "
                f"{counts['incidents']} incident, {counts['actions']} action, "
                f"{counts['audit_entries']} audit entry, {counts['backup_jobs']} backup job, "
                f"{counts['backup_anomalies']} backup anomaly, {counts['heartbeats']} heartbeat. "
                + (f"Không restart được {', '.join(failed)}; cần restart thủ công."
                   if failed else "Watcher/Worker đã khởi động lại.")
            ),
        ),
    )


@router.post("/clusters/{cluster_id}/backup-config", response_class=HTMLResponse)
async def update_cluster_backup_config(
    request: Request,
    cluster_id: str,
    user: str = Depends(require_login),
    backup_enabled: str = Form(""),
    backup_tracked_images: str = Form(""),
    backup_full_refresh_days: str = Form(""),
    backup_transport: str = Form(""),
    backup_ssh_host: str = Form(""),
    backup_ssh_user: str = Form(""),
    backup_ssh_key_path: str = Form(""),
    backup_ssh_landing_dir: str = Form(""),
    backup_s3_endpoint: str = Form(""),
    backup_s3_access_key: str = Form(""),
    backup_s3_secret_key: str = Form(""),
    backup_s3_bucket: str = Form(""),
    backup_immutable_enabled: str = Form(""),
    backup_immutable_lock_days: str = Form(""),
):
    """Multi-tenant remediation Phase 3 — narrowly-scoped edit endpoint for
    an EXISTING additional cluster's backup config ONLY. `create_cluster()`
    above has no general "edit a cluster" counterpart (its connection
    fields — MON nodes, SSH creds, exec mode — stay create-time-only,
    unchanged by this phase); this endpoint exists purely because getting
    ~10 backup-target fields right in ONE create-time submission is
    unrealistic in practice.

    `backup_ssh_key_path`/`backup_s3_access_key`/`backup_s3_secret_key` are
    secret-shaped — the template never echoes a saved secret back (same
    posture `telegram_bot_token`'s own `<input type=password>` already
    has on the create form), so these three are write-only: submitted
    blank means "keep the current value", not "clear it". Every other
    field is a plain overwrite, including `backup_enabled`/
    `backup_transport` themselves (an admin unchecking "Bật backup" here
    genuinely disables it — worker/backup/scheduler.py's next rebuild
    (via restart_worker() below) simply stops registering this cluster's
    jobs)."""
    _require_admin_privilege(user)

    submitted = {
        "backup_enabled": backup_enabled.strip().lower() in ("true", "on", "1"),
        "backup_tracked_images": backup_tracked_images.strip(),
        "backup_full_refresh_days": backup_full_refresh_days.strip(),
        "backup_transport": backup_transport.strip(),
        "backup_ssh_host": backup_ssh_host.strip(),
        "backup_ssh_user": backup_ssh_user.strip(),
        "backup_ssh_key_path": backup_ssh_key_path.strip(),
        "backup_ssh_landing_dir": backup_ssh_landing_dir.strip(),
        "backup_s3_endpoint": backup_s3_endpoint.strip(),
        "backup_s3_access_key": backup_s3_access_key.strip(),
        "backup_s3_secret_key": backup_s3_secret_key.strip(),
        "backup_s3_bucket": backup_s3_bucket.strip(),
        "backup_immutable_enabled": backup_immutable_enabled.strip().lower() in ("true", "on", "1"),
        "backup_immutable_lock_days": backup_immutable_lock_days.strip(),
    }

    if submitted["backup_transport"] not in VALID_BACKUP_TRANSPORTS:
        return templates.TemplateResponse(
            request,
            "clusters.html",
            _clusters_context(
                user,
                backup_config_error=f"Nơi lưu backup không hợp lệ: {submitted['backup_transport']!r}",
                backup_config_cluster_id=cluster_id,
                backup_config_form_values=submitted,
            ),
        )
    if submitted["backup_enabled"] and not submitted["backup_transport"]:
        return templates.TemplateResponse(
            request,
            "clusters.html",
            _clusters_context(
                user,
                backup_config_error="Bật backup cần chọn nơi lưu (SSH hoặc S3).",
                backup_config_cluster_id=cluster_id,
                backup_config_form_values=submitted,
            ),
        )

    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None or cluster.is_default:
            return templates.TemplateResponse(
                request,
                "clusters.html",
                _clusters_context(user, backup_config_error="Không tìm thấy cụm, hoặc đây là cụm mặc định."),
            )

        cluster.backup_enabled = submitted["backup_enabled"]
        cluster.backup_tracked_images = submitted["backup_tracked_images"]
        cluster.backup_full_refresh_days = (
            int(submitted["backup_full_refresh_days"]) if submitted["backup_full_refresh_days"].isdigit() else None
        )
        cluster.backup_transport = submitted["backup_transport"]
        cluster.backup_ssh_host = submitted["backup_ssh_host"]
        cluster.backup_ssh_user = submitted["backup_ssh_user"]
        if submitted["backup_ssh_key_path"]:
            cluster.backup_ssh_key_path = submitted["backup_ssh_key_path"]
        cluster.backup_ssh_landing_dir = submitted["backup_ssh_landing_dir"]
        cluster.backup_s3_endpoint = submitted["backup_s3_endpoint"]
        if submitted["backup_s3_access_key"]:
            cluster.backup_s3_access_key = submitted["backup_s3_access_key"]
        if submitted["backup_s3_secret_key"]:
            cluster.backup_s3_secret_key = submitted["backup_s3_secret_key"]
        cluster.backup_s3_bucket = submitted["backup_s3_bucket"]
        cluster.backup_immutable_enabled = submitted["backup_immutable_enabled"]
        cluster.backup_immutable_lock_days = (
            int(submitted["backup_immutable_lock_days"]) if submitted["backup_immutable_lock_days"].isdigit() else 7
        )
        session.commit()
        cluster_name = cluster.name

    # Not restart_watcher() -- backup config only affects Worker's own
    # scheduler (worker/backup/scheduler.py::build_scheduler(), which
    # enumerates clusters once at ITS OWN startup, same shape Watcher's
    # poll-thread startup already has, see create_cluster()'s own comment).
    worker_result = await asyncio.to_thread(restart_worker)
    if not worker_result.get("restarted"):
        return templates.TemplateResponse(
            request, "clusters.html", _clusters_context(
                user, backup_config_error=(
                    f"Đã lưu backup cho cụm {cluster_name!r}, nhưng không restart được Worker; "
                    "cần restart thủ công để scheduler nhận cấu hình mới."
                ),
                backup_config_cluster_id=cluster_id,
            )
        )

    return templates.TemplateResponse(
        request,
        "clusters.html",
        _clusters_context(
            user, backup_config_success=f"Đã lưu cấu hình backup cho cụm {cluster_name!r} — Worker đã khởi động lại."
        ),
    )
