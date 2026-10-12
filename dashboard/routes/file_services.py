"""File & object services: CephFS, NFS and RGW of the selected cluster (11/10/2026).

Plan/in-progress/croit-parity-operations-plan-2026-10-10.md, C5. Shows the
filesystems (MDS state, subvolumes), NFS clusters and exports, and RGW
services, and proposes the five RISKY actions that add to them
(worker/executor/commands.py): every change waits for an operator's approval.

The state is read in two batched remote shells (each `cephadm shell` costs
seconds) and cached; a stale view is refreshed in the background, as the
Trash page does since 09/10/2026.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config.settings import settings
from dashboard.cluster_scope import cluster_connection, selected_cluster
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import audit, db
from shared.ceph_query_cache import get_cached, schedule_refresh, store
from shared.models import Action, ActionStatus, Cluster, Incident, IncidentStatus
from shared.time import utc_now
from watcher.ceph_client import CephQueryError, run_ceph_json_batch_command_with
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()

ACTION_IDS = ("cephfs_create_volume", "cephfs_create_subvolume", "nfs_create_cluster", "nfs_create_export",
              "nfs_delete_export",
              "rgw_apply_service")
CEPHADM_ONLY = {"nfs_create_cluster", "nfs_create_export", "nfs_delete_export", "rgw_apply_service"}
CEPH_CODE = "FILE_SERVICE_CHANGE"
CACHE_NAMESPACE = "file-services"
CACHE_TTL_SECONDS = 60
CACHE_MAX_AGE_SECONDS = 6 * 3600
_IN_FLIGHT = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value)
_ACTION_LABELS = {
    "cephfs_create_volume": "Tạo CephFS", "cephfs_create_subvolume": "Tạo subvolume",
    "nfs_create_cluster": "Tạo cụm NFS", "nfs_create_export": "Tạo NFS export", "nfs_delete_export": "Gỡ NFS export", "rgw_apply_service": "Đặt dịch vụ RGW",
}


def _exec_mode(cluster: Cluster) -> str:
    return settings.ceph_exec_mode if cluster.is_default else (cluster.ceph_exec_mode or "none")


def _batch(cluster: Cluster, commands: list[str]) -> list:
    _host, payloads = run_ceph_json_batch_command_with(*cluster_connection(cluster), commands, parallel=True)
    return payloads


def load_state(cluster: Cluster) -> dict:
    """Filesystems, NFS and RGW of ``cluster`` in two remote shells; missing parts are None."""
    cephadm = _exec_mode(cluster) == "cephadm"
    first = ["ceph fs ls --format json", "ceph nfs cluster ls --format json"]
    if cephadm:
        first += ["ceph orch ls rgw --format json", "ceph orch host ls --format json"]
    fs_list, nfs_ids, rgw, hosts = (_batch(cluster, first) + [None, None])[:4]
    filesystems = [item for item in fs_list or [] if isinstance(item, dict)]
    nfs = [str(item) for item in nfs_ids or [] if isinstance(item, str)]
    second = []
    for fs in filesystems:
        name = shlex.quote(str(fs["name"]))
        second += [f"ceph fs status {name} --format json", f"ceph fs subvolume ls {name} --format json"]
    second += [f"ceph nfs export ls {shlex.quote(nfs_id)} --detailed --format json" for nfs_id in nfs]
    details = _batch(cluster, second) if second else []
    for index, fs in enumerate(filesystems):
        status = details[2 * index] or {}
        fs["mds"] = [{"name": m.get("name"), "state": m.get("state"), "rank": m.get("rank")}
                     for m in status.get("mdsmap") or [] if isinstance(m, dict)]
        fs["subvolumes"] = [s.get("name") for s in details[2 * index + 1] or [] if isinstance(s, dict)]
    exports_at = 2 * len(filesystems)
    return {
        "filesystems": filesystems,
        "nfs": [{"id": nfs_id, "exports": [
            {"pseudo": e.get("pseudo"), "path": e.get("path"), "fs": (e.get("fsal") or {}).get("fs_name"),
             "access": e.get("access_type")}
            for e in details[exports_at + index] or [] if isinstance(e, dict)]} for index, nfs_id in enumerate(nfs)],
        "rgw": [{"name": svc.get("service_name"), "placement": svc.get("placement"),
                 "running": (svc.get("status") or {}).get("running"), "size": (svc.get("status") or {}).get("size")}
                for svc in rgw or [] if isinstance(svc, dict)],
        "hosts": [str(h.get("hostname")) for h in hosts or [] if isinstance(h, dict)],
        "cephadm": cephadm,
    }


def _state(cluster: Cluster) -> tuple[dict | None, str | None, float | None]:
    """(state, error, age seconds): cached up to six hours, refreshed in the background once stale."""
    key = str(cluster.id)
    cached = get_cached(CACHE_NAMESPACE, key, max_age_seconds=CACHE_MAX_AGE_SECONDS)
    if cached is not None:
        value, age = cached
        if age > CACHE_TTL_SECONDS:
            schedule_refresh(CACHE_NAMESPACE, key, lambda: load_state(cluster), CACHE_TTL_SECONDS)
        return value if isinstance(value, dict) else None, None, age
    try:
        value = load_state(cluster)
    except CephQueryError as exc:
        return None, str(exc), None
    store(CACHE_NAMESPACE, key, value)
    return value, None, 0.0


def _actions(cluster: Cluster) -> list[dict]:
    with db.SessionLocal() as session:
        scope = Incident.cluster_id == cluster.id
        if cluster.is_default:
            scope = scope | Incident.cluster_id.is_(None)
        rows = (session.query(Action).join(Incident, Incident.id == Action.incident_id)
                .filter(Action.action_id.in_(ACTION_IDS), scope).order_by(Action.created_at.desc()).limit(15).all())
        return [{"id": a.id, "label": _ACTION_LABELS.get(a.action_id, a.action_id), "status": a.status,
                 "command": a.proposed_command, "created_at": a.created_at} for a in rows]


@router.get("/file-services", response_class=HTMLResponse)
async def file_services_page(request: Request, user: str = Depends(require_login)):
    cluster = selected_cluster(request)
    state, error, age = await asyncio.to_thread(_state, cluster)
    return templates.TemplateResponse(request, "file_services.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "cluster_name": cluster.name,
        "exec_mode": _exec_mode(cluster), "state": state, "error": error, "age_seconds": age,
        "actions": await asyncio.to_thread(_actions, cluster),
    })


def _clean_params(action_id: str, body: dict) -> dict:
    keys = {"cephfs_create_volume": ("fs_name",), "cephfs_create_subvolume": ("fs_name", "subvolume", "size_gib"),
            "nfs_create_cluster": ("nfs_id", "hosts"),
            "nfs_create_export": ("nfs_id", "fs_name", "pseudo_path", "subvolume", "path", "readonly"),
            "nfs_delete_export": ("nfs_id", "pseudo_path"),
            "rgw_apply_service": ("service_id", "hosts", "port")}[action_id]
    params = {key: body[key] for key in keys if body.get(key) not in (None, "", [])}
    for key in ("size_gib", "port"):
        if key in params:
            try:
                params[key] = int(params[key])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"{key} phải là số nguyên.") from exc
    if action_id == "nfs_create_export":
        if params.get("subvolume"):
            params.pop("path", None)  # the Worker exports the subvolume's own path
        else:
            params.setdefault("path", "/")
        params["readonly"] = body.get("readonly") is True
    return params


@router.post("/file-services/propose")
async def propose_file_service(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được đề xuất thay đổi dịch vụ.")
    cluster = selected_cluster(request)
    body = await request.json()
    action_id = str(body.get("action_id") or "")
    if action_id not in ACTION_IDS:
        raise HTTPException(status_code=400, detail="Thao tác không hợp lệ.")
    if action_id in CEPHADM_ONLY and _exec_mode(cluster) != "cephadm":
        raise HTTPException(status_code=400, detail="NFS và RGW qua orchestrator chỉ hỗ trợ cụm cephadm.")
    params = _clean_params(action_id, body)
    mon_nodes = cluster_connection(cluster)[0]
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="Cụm chưa cấu hình MON node.")
    try:
        command = executor_commands.get_command(action_id, mon_nodes[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Thông tin không hợp lệ: {exc}") from exc
    with db.SessionLocal() as session:
        incident = Incident(cluster_id=cluster.id, ceph_code=f"{CEPH_CODE}:{action_id}",
                            status=IncidentStatus.PENDING_APPROVAL.value, detected_at=utc_now(),
                            log_excerpt=f"{user} đề xuất {_ACTION_LABELS[action_id]} trên cụm {cluster.name!r}")
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id, action_id=action_id, classification=gate.classify_action(action_id).value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=f"{_ACTION_LABELS[action_id]} trên cụm {cluster.name}: {json.dumps(params, ensure_ascii=False)}",
            target_nodes=json.dumps([mon_nodes[0]]), action_params=json.dumps(params), proposed_command=command,
            expires_at=utc_now() + timedelta(hours=max(1, settings.action_approval_expiry_hours)),
        )
        session.add(action)
        session.flush()
        audit.record(session, incident_id=incident.id, action_id=action.id,
                     event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL, actor=user)
        session.commit()
        return JSONResponse({"action_id": action.id}, status_code=201)
