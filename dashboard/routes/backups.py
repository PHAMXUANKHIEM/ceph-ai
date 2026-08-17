"""Backup real-time visibility (Story 9.6, PRD FR-16/17) — reads the SAME
state `worker/backup/engine.py`/`metadata.py`/`restore_drill.py` already
write (`Action.execution_progress`, `BackupJob`). Route-only reads via
`shared/models.py` — never imports `worker/backup/engine.py`,
`scheduler.py`, or `storage/` (AD-3). Reading `worker/backup/
policy_config.py`'s YAML loader is fine (config, not execution — see this
story's own Dev Notes), same as `volumes.py` importing `worker.policy.gate`
for a similarly narrow, non-executing purpose.

Real-time updates use client-side `setInterval`+`fetch()` against
`/api/backups/progress` — the SAME pattern `deploy_cluster.js`/
`volume_perf_sweep.js` already established, NOT `dashboard/ws.py` (that
now only serves the original Incident feed, Story 1.5 — Architecture AD-12
was corrected to reflect this after reading the real codebase).

Also renders Story 9.5's `BackupDigestLog`/`BackupAnomaly` rows (digest.py/
anomaly.py write them; this route only reads — same AD-3 boundary). Their
own docstrings note Dashboard display was left for this story to pick up.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.routes import auth
from dashboard.cluster_scope import cluster_connection, cluster_selection, selected_cluster
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from dashboard.vntime import format_vn_clock
from config.settings import settings
from shared import audit, db
from shared.cluster_nodes import configured_nodes
from sqlalchemy import or_
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    BackupAnomaly,
    BackupDigestLog,
    BackupJob,
    Incident,
    IncidentStatus,
)
from worker.backup.policy_config import load_backup_policy
from worker.backup.cluster_scope import parse_tracked_images
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate
from watcher import ceph_client
from watcher.ceph_client import CephQueryError

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# 2026-07-31 (Story 9.7): restore_rbd_image_to_production added — unlike
# retention_sweep_delete/backup_delete_manual (still no live progress),
# `worker/backup/engine.py::_run_restore_to_production` DOES write
# Action.execution_progress (a single "restore" step), so it belongs here
# too — otherwise the progress panel below would never show it running.
BACKUP_PROGRESS_ACTION_IDS = (
    "rbd_backup_run",
    "backup_metadata_run",
    "restore_drill_execute",
    "restore_rbd_image_to_production",
    "restore_rbd_image_as_new",
)
# Same constant, same values, as dashboard/routes/volumes.py|deploy_cluster.py|
# patch.py|upgrade.py|delete_cluster.py|convert_cluster.py — no shared helper
# module for it in this codebase, matching that established precedent.
_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)
HISTORY_LIMIT_PER_IMAGE = 30
# Story 9.5's digest/anomaly rows are informational, not per-image history —
# a flat "most recent N overall" list (unlike `_history` above, which is
# per tracked image) is enough for the Dashboard, same rationale as the
# progress panel only ever showing the single latest running job.
DIGEST_LIMIT = 10
ANOMALY_LIMIT = 20

# Synthetic Incident.ceph_code — same trick every other propose route in
# this project uses (AuditEntry.incident_id is a required FK, and this has
# no real detected Incident behind it, only an operator explicitly picking
# a tracked image and clicking "Khôi phục").
RESTORE_CEPH_CODE = "RESTORE_RBD_IMAGE_TO_PRODUCTION"
RESTORE_ACTION_ID = "restore_rbd_image_to_production"
RESTORE_AS_NEW_ACTION_ID = "restore_rbd_image_as_new"
MANUAL_BACKUP_CEPH_CODE = "BACKUP_MANUAL"
RESTORE_AS_NEW_CEPH_CODE = "RESTORE_RBD_IMAGE_AS_NEW"
_RBD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ tài khoản admin được vận hành backup.")


def _job_scope(column, cluster):
    return or_(column == cluster.id, column.is_(None)) if cluster.is_default else column == cluster.id


def _latest_running_backup_action(cluster=None) -> Action | None:
    with db.SessionLocal() as session:
        return (
            session.query(Action).join(Incident, Action.incident_id == Incident.id)
            .filter(
                Action.action_id.in_(BACKUP_PROGRESS_ACTION_IDS),
                Action.status.in_(_IN_FLIGHT_ACTION_STATUSES),
                _job_scope(Incident.cluster_id, cluster) if cluster is not None else True,
            )
            .order_by(Action.created_at.desc())
            .first()
        )


def _pending_restore_action(cluster=None) -> Action | None:
    with db.SessionLocal() as session:
        return (
            session.query(Action).join(Incident, Action.incident_id == Incident.id)
            .filter(Action.action_id.in_((RESTORE_ACTION_ID, RESTORE_AS_NEW_ACTION_ID)), Action.status.in_(_IN_FLIGHT_ACTION_STATUSES),
                    _job_scope(Incident.cluster_id, cluster) if cluster is not None else True)
            .order_by(Action.created_at.desc())
            .first()
        )


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


def _tracked_images(cluster=None) -> list[dict]:
    if cluster is not None and not cluster.is_default:
        return [{"pool": pool, "image": image} for pool, image in parse_tracked_images(cluster.backup_tracked_images)]
    return [t for t in (load_backup_policy().get("tracked_images") or []) if t.get("pool") and t.get("image")]


def _queue(tracked: list[dict], cluster=None) -> list[dict]:
    """AC #4: which tracked image is due next — the one that's gone
    longest without a successful run sorts first (None = never run at
    all, sorts before any real timestamp). `last_run_at` stays a real
    `datetime` (not a pre-formatted string) — `backups.html` renders it
    with the same `|vntime` Jinja filter every other template uses."""
    entries = []
    with db.SessionLocal() as session:
        for t in tracked:
            pool, image = t["pool"], t["image"]
            latest = (
                session.query(BackupJob)
                .filter(
                    BackupJob.pool == pool,
                    BackupJob.image == image,
                    BackupJob.job_type.in_(("full", "incremental")),
                    _job_scope(BackupJob.cluster_id, cluster) if cluster is not None else True,
                )
                .order_by(BackupJob.created_at.desc())
                .first()
            )
            latest_success = (
                session.query(BackupJob)
                .filter(
                    BackupJob.pool == pool,
                    BackupJob.image == image,
                    BackupJob.job_type.in_(("full", "incremental")),
                    BackupJob.status == "SUCCESS",
                    _job_scope(BackupJob.cluster_id, cluster) if cluster is not None else True,
                )
                .order_by(BackupJob.created_at.desc())
                .first()
            )
            entries.append(
                {
                    "pool": pool,
                    "image": image,
                    "last_run_at": latest_success.created_at if latest_success else None,
                    "last_status": latest.status if latest else None,
                }
            )
    entries.sort(key=lambda e: e["last_run_at"] or datetime.min)
    return entries


def _history(tracked: list[dict], cluster=None) -> list[dict]:
    """AC #4: >= `HISTORY_LIMIT_PER_IMAGE` most recent runs PER (pool,
    image) — Story 9.1's `Index(pool, image, created_at)` (the same one
    Story 9.5's anomaly baseline already uses) makes this cheap. Includes
    `job_type="restore_drill"` rows too, distinguished by that column, not
    filtered separately per (pool, image) like full/incremental."""
    rows_out: list[dict] = []
    with db.SessionLocal() as session:
        for t in tracked:
            pool, image = t["pool"], t["image"]
            rows = (
                session.query(BackupJob)
                .filter(BackupJob.pool == pool, BackupJob.image == image,
                        _job_scope(BackupJob.cluster_id, cluster) if cluster is not None else True)
                .order_by(BackupJob.created_at.desc())
                .limit(HISTORY_LIMIT_PER_IMAGE)
                .all()
            )
            for row in rows:
                rows_out.append(
                    {
                        "pool": row.pool,
                        "image": row.image,
                        "job_type": row.job_type,
                        "status": row.status,
                        "duration_seconds": row.duration_seconds,
                        "size_bytes": row.size_bytes,
                        "created_at": row.created_at,
                        "error_message": row.error_message,
                    }
                )
    rows_out.sort(key=lambda h: h["created_at"] or datetime.min, reverse=True)
    return rows_out


def _digests(cluster=None) -> list[dict]:
    """Story 9.5 (PRD FR-14): most recent BackupDigestLog rows, newest
    first — read-only, same as `_history` (route never imports
    `worker/backup/digest.py`, only the model it wrote to, per AD-3)."""
    with db.SessionLocal() as session:
        if cluster is not None and not cluster.is_default:
            return []
        rows = session.query(BackupDigestLog).order_by(BackupDigestLog.created_at.desc()).limit(DIGEST_LIMIT).all()
        return [
            {
                "period_start": row.period_start,
                "period_end": row.period_end,
                "succeeded_count": row.succeeded_count,
                "failed_count": row.failed_count,
                "anomaly_count": row.anomaly_count,
                "summary_text": row.summary_text,
                "created_at": row.created_at,
            }
            for row in rows
        ]


def _anomalies(cluster=None) -> list[dict]:
    """Story 9.5 (PRD FR-15): most recent BackupAnomaly rows, newest first
    — joined against BackupJob for pool/image/job_type context, since
    BackupAnomaly itself only stores the FK (`backup_job_id`)."""
    with db.SessionLocal() as session:
        rows = (
            session.query(BackupAnomaly, BackupJob)
            .join(BackupJob, BackupAnomaly.backup_job_id == BackupJob.id)
            .filter(_job_scope(BackupJob.cluster_id, cluster) if cluster is not None else True)
            .order_by(BackupAnomaly.created_at.desc())
            .limit(ANOMALY_LIMIT)
            .all()
        )
        return [
            {
                "pool": job.pool,
                "image": job.image,
                "job_type": job.job_type,
                "kind": anomaly.kind,
                "severity": anomaly.severity,
                "ai_summary": anomaly.ai_summary,
                "created_at": anomaly.created_at,
            }
            for anomaly, job in rows
        ]


@router.get("/backups", response_class=HTMLResponse)
async def index(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    tracked = _tracked_images(cluster)
    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "queue": _queue(tracked, cluster),
            "history": _history(tracked, cluster),
            "digests": _digests(cluster),
            "anomalies": _anomalies(cluster),
            "tracked_images": tracked,
            "pending_restore_action": _pending_restore_action(cluster),
            "clusters": clusters,
            "selected_cluster": cluster,
        },
    )


@router.post("/backups/restore/propose")
async def propose_restore(request: Request, user: str = Depends(require_login)):
    """Story 9.7 Task 3 UI — proposes `restore_rbd_image_to_production`
    for ONE tracked image, restoring it OVER its own live production data.
    Always Risky (action_policy.yaml) — needs a separate Dashboard approval
    (`/actions/{id}/approve`) before Worker ever touches anything, same as
    every other cluster/backup-lifecycle propose route."""
    _require_admin_privilege(user)
    body = await request.json()
    pool = str(body.get("pool", "")).strip()
    image = str(body.get("image", "")).strip()

    cluster = selected_cluster(request)
    tracked = _tracked_images(cluster)
    if not any(t["pool"] == pool and t["image"] == image for t in tracked):
        raise HTTPException(
            status_code=400,
            detail=f"{pool}/{image} không nằm trong tracked_images đã cấu hình — không thể đề xuất khôi phục",
        )

    with db.SessionLocal() as session:
        has_full_backup = (
            session.query(BackupJob.id)
            .filter(
                BackupJob.pool == pool,
                BackupJob.image == image,
                BackupJob.job_type == "full",
                BackupJob.status == "SUCCESS",
                _job_scope(BackupJob.cluster_id, cluster),
            )
            .first()
            is not None
        )
    if not has_full_backup:
        raise HTTPException(
            status_code=409,
            detail=f"Chưa có bản full backup thành công cho {pool}/{image} — không thể khôi phục.",
        )

    # target_nodes MUST be a non-empty single-host list — same requirement
    # worker/backup/scheduler.py::_create_scheduled_action's docstring
    # documents (a previously-shipped bug: _execute_approved_action rejects
    # an empty/missing target_nodes as "malformed" and marks the Action
    # FAILED before ever reaching engine.py, which never reads this column
    # itself — it resolves its own mon node from settings).
    mon_nodes = [n["host"] for n in configured_nodes(None if cluster.is_default else cluster) if "MON" in n["roles"]]
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="ceph_mon_nodes chưa được cấu hình")

    action_params = {"pool": pool, "image": image}
    try:
        preview_command = executor_commands.get_command(RESTORE_ACTION_ID, None, action_params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        existing = (
            session.query(Action)
            .filter(Action.action_id == RESTORE_ACTION_ID)
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="Đã có một đề xuất khôi phục volume đang chờ duyệt hoặc đã duyệt — không thể tạo thêm.",
            )

        incident = Incident(
            cluster_id=None if cluster.is_default else cluster.id,
            ceph_code=RESTORE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất khôi phục {pool}/{image} từ backup (ghi đè dữ liệu hiện tại) bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=RESTORE_ACTION_ID,
            classification=gate.classify_action(RESTORE_ACTION_ID).value,  # always RISKY (AD-5)
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"Khôi phục {pool}/{image} từ bản backup full gần nhất + toàn bộ chain "
                f"export-diff — GHI ĐÈ dữ liệu hiện tại của image này. Chỉ dùng khi image này bị "
                f"hỏng/mất dữ liệu; không cần dựng lại cả cụm (khác với trang Restore Cluster)."
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


@router.post("/backups/restore-as-new/propose")
async def propose_restore_as_new(request: Request, user: str = Depends(require_login)):
    """Restore the latest verified chain into a new RBD image.

    This is the safe default restore path: the source image is never mutated.
    Live destination existence/capacity checks are repeated by Ceph itself at
    execution time because ``rbd import`` fails closed if the name appears in
    the approval gap.
    """
    _require_admin_privilege(user)
    body = await request.json()
    pool = str(body.get("pool", "")).strip()
    image = str(body.get("image", "")).strip()
    dest_pool = str(body.get("dest_pool", "")).strip()
    dest_image = str(body.get("dest_image", "")).strip()
    if not all(_RBD_NAME_RE.fullmatch(value) for value in (pool, image, dest_pool, dest_image)):
        raise HTTPException(status_code=400, detail="Pool/image chỉ được chứa chữ, số, dấu chấm, gạch dưới hoặc gạch ngang.")
    if pool == dest_pool and image == dest_image:
        raise HTTPException(status_code=400, detail="Volume đích phải khác volume nguồn.")

    cluster = selected_cluster(request)
    if not any(t["pool"] == pool and t["image"] == image for t in _tracked_images(cluster)):
        raise HTTPException(status_code=400, detail="Image nguồn không nằm trong tracked_images.")
    with db.SessionLocal() as session:
        full_job = (
            session.query(BackupJob)
            .filter(
                BackupJob.pool == pool,
                BackupJob.image == image,
                BackupJob.job_type == "full",
                BackupJob.status == "SUCCESS",
                _job_scope(BackupJob.cluster_id, cluster),
            )
            .order_by(BackupJob.created_at.desc())
            .first()
        )
    if full_job is None:
        raise HTTPException(status_code=409, detail="Chưa có bản full backup thành công để khôi phục.")

    try:
        inventory = (
            ceph_client.query_rbd_inventory(dest_pool)
            if cluster.is_default
            else ceph_client.query_rbd_inventory_with(dest_pool, *cluster_connection(cluster))
        )
        overview = (
            ceph_client.query_rbd_pool_overview(dest_pool)
            if cluster.is_default
            else ceph_client.query_rbd_pool_overview_with(dest_pool, *cluster_connection(cluster))
        )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không kiểm tra được pool đích: {exc}") from exc
    if any(row.get("name") == dest_image for row in inventory):
        raise HTTPException(status_code=409, detail=f"Volume đích {dest_pool}/{dest_image} đã tồn tại.")
    max_available = int(overview.get("max_available") or 0)
    if max_available and int(full_job.size_bytes or 0) > max_available:
        raise HTTPException(status_code=409, detail="Pool đích không đủ dung lượng cho bản full backup.")

    mon_nodes = [n["host"] for n in configured_nodes(None if cluster.is_default else cluster) if "MON" in n["roles"]]
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="ceph_mon_nodes chưa được cấu hình")
    params = {"pool": pool, "image": image, "dest_pool": dest_pool, "dest_image": dest_image}
    try:
        preview_command = executor_commands.get_command(RESTORE_AS_NEW_ACTION_ID, None, params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}") from exc

    with db.SessionLocal() as session:
        existing = (
            session.query(Action)
            .join(Incident, Action.incident_id == Incident.id)
            .filter(
                Action.action_id.in_((RESTORE_ACTION_ID, RESTORE_AS_NEW_ACTION_ID)),
                Action.status.in_(_IN_FLIGHT_ACTION_STATUSES),
                _job_scope(Incident.cluster_id, cluster),
            )
            .first()
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Đã có một restore đang chờ duyệt hoặc thực thi trên cluster này.")
        incident = Incident(
            cluster_id=None if cluster.is_default else cluster.id,
            ceph_code=RESTORE_AS_NEW_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất restore {pool}/{image} thành {dest_pool}/{dest_image} bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=RESTORE_AS_NEW_ACTION_ID,
            classification=gate.classify_action(RESTORE_AS_NEW_ACTION_ID).value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"Khôi phục {pool}/{image} từ full backup gần nhất và chain incremental "
                f"sang volume mới {dest_pool}/{dest_image}; không thay đổi volume nguồn."
            ),
            target_nodes=json.dumps([mon_nodes[0]]),
            action_params=json.dumps(params),
            proposed_command=preview_command,
        )
        session.add(action)
        session.flush()
        audit.record(session, incident_id=incident.id, action_id=action.id,
                     event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL, actor=user)
        session.commit()
        action_pk = action.id
    return JSONResponse({"action_id": action_pk}, status_code=201)


def _create_manual_backup_action(action_id: str, action_params: dict, user: str, cluster=None) -> str:
    mon_nodes = [n["host"] for n in configured_nodes(None if cluster is None or cluster.is_default else cluster) if "MON" in n["roles"]]
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="ceph_mon_nodes chưa được cấu hình")

    with db.SessionLocal() as session:
        existing = (
            session.query(Action)
            .filter(
                Action.action_id.in_(("rbd_backup_run", "backup_metadata_run")),
                Action.status.in_(_IN_FLIGHT_ACTION_STATUSES),
            )
            .first()
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Đã có một backup đang chờ hoặc đang chạy.")

        label = (
            f"Backup RBD thủ công cho {action_params['pool']}/{action_params['image']}"
            if action_id == "rbd_backup_run"
            else "Backup metadata cụm thủ công"
        )
        incident = Incident(
            cluster_id=None if cluster is None or cluster.is_default else cluster.id,
            ceph_code=MANUAL_BACKUP_CEPH_CODE,
            status=IncidentStatus.EXECUTING.value,
            log_excerpt=f"{label} bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.APPROVED.value,
            rationale=label,
            target_nodes=json.dumps([mon_nodes[0]]),
            action_params=json.dumps(action_params),
        )
        session.add(action)
        session.flush()
        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_BACKUP_MANUAL_REQUESTED,
            actor=user,
        )
        session.commit()
        return action.id


@router.post("/backups/run-now")
async def run_backup_now(request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    body = await request.json()
    pool = str(body.get("pool", "")).strip()
    image = str(body.get("image", "")).strip()
    cluster = selected_cluster(request)
    if not any(t["pool"] == pool and t["image"] == image for t in _tracked_images(cluster)):
        raise HTTPException(status_code=400, detail="Image không nằm trong tracked_images.")
    action_pk = _create_manual_backup_action("rbd_backup_run", {"pool": pool, "image": image}, user, cluster)
    return JSONResponse({"action_id": action_pk}, status_code=201)


@router.post("/backups/metadata/run-now")
async def run_metadata_backup_now(request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    cluster = selected_cluster(request)
    action_pk = _create_manual_backup_action("backup_metadata_run", {}, user, cluster)
    return JSONResponse({"action_id": action_pk}, status_code=201)


@router.get("/api/backups/progress")
async def backups_progress_api(request: Request, user: str = Depends(require_login)):
    action = _latest_running_backup_action(selected_cluster(request))
    if action is None:
        return {"action_id": None, "status": None, "progress": []}
    try:
        progress = json.loads(action.execution_progress) if action.execution_progress else []
    except (TypeError, ValueError):
        progress = []
    progress = _with_step_display_times(progress)
    return {"action_id": action.action_id, "status": action.status, "progress": progress}
