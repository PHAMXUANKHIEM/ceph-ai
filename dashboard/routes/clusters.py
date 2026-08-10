import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.settings import restart_watcher
from dashboard.templating import make_templates
from shared import db
from shared.clusters import ensure_default_cluster
from shared.models import Cluster
from watcher.ceph_client import VALID_EXEC_MODES, CephQueryError, query_cluster_health_with

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
    cluster_form_values: dict | None = None,
) -> dict:
    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": _list_clusters(),
        "cluster_create_error": cluster_create_error,
        "cluster_create_success": cluster_create_success,
        "cluster_toggle_error": cluster_toggle_error,
        "cluster_form_values": cluster_form_values or {},
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
    ceph_container_name: str = Form(""),
    ssh_user: str = Form(""),
    ssh_key_path: str = Form(""),
    ceph_exec_mode: str = Form("docker"),
):
    """Adds an additional observed cluster — tests the connection with the
    submitted values BEFORE saving (same AC as /settings' cluster form,
    reusing the same watcher/ceph_client.query_cluster_health_with helper),
    so a typo'd MON node/SSH key never silently gets saved as if it worked."""
    _require_admin_privilege(user)

    submitted = {
        "name": name.strip(),
        "ceph_mon_nodes": ceph_mon_nodes.strip(),
        "ceph_container_name": ceph_container_name.strip(),
        "ssh_user": ssh_user.strip(),
        "ssh_key_path": ssh_key_path.strip(),
        "ceph_exec_mode": ceph_exec_mode.strip() or "docker",
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
                ceph_container_name=submitted["ceph_container_name"],
                ssh_user=submitted["ssh_user"],
                ssh_key_path=submitted["ssh_key_path"],
                ceph_exec_mode=submitted["ceph_exec_mode"],
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
    # has (Alert Telegram, Settings' own "Kết nối cụm Ceph").
    await asyncio.to_thread(restart_watcher)

    return templates.TemplateResponse(
        request,
        "clusters.html",
        _clusters_context(
            user,
            cluster_create_success=f"Đã thêm cụm {submitted['name']!r} — Watcher đã khởi động lại để bắt đầu giám sát ngay.",
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
    await asyncio.to_thread(restart_watcher)

    return templates.TemplateResponse(request, "clusters.html", _clusters_context(user))
