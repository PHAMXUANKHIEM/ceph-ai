"""Read-only, cluster-scoped audit viewer for storage and data-protection work.

The endpoint deliberately returns metadata only. Preview text, command output,
credentials and raw backend errors remain behind the owning feature's audit
page or are represented by ``details_available``.
"""

from __future__ import annotations

import math
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import or_

from dashboard.cluster_scope import cluster_selection, selected_cluster
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import AuditEntry, BackupJob, Incident, ObjectStorageAuditEntry


router = APIRouter(tags=["storage-audit"])
templates = make_templates()
PAGE_SIZE_DEFAULT = 25
PAGE_SIZE_MAX = 100
MAX_ROWS_PER_SOURCE = 500


def _scope(column, cluster) -> object:
    if cluster.is_default:
        return or_(column == cluster.id, column.is_(None))
    return column == cluster.id


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _rows(cluster, kind: str = "") -> list[dict]:
    rows: list[dict] = []
    with db.SessionLocal() as session:
        if not kind or kind == "object_storage":
            for row in session.query(ObjectStorageAuditEntry).filter(
                ObjectStorageAuditEntry.cluster_id == cluster.id,
            ).order_by(ObjectStorageAuditEntry.created_at.desc()).limit(MAX_ROWS_PER_SOURCE).all():
                rows.append({
                    "kind": "object_storage",
                    "id": row.id,
                    "actor": row.actor,
                    "event": row.action,
                    "target": f"{row.target_type}:{row.target_id}",
                    "result": row.result,
                    "created_at": _iso(row.created_at),
                    "request_id": row.id,
                    "details_available": bool(row.preview or row.error_message),
                })

        if not kind or kind == "incident":
            query = session.query(AuditEntry, Incident).join(
                Incident, AuditEntry.incident_id == Incident.id,
            ).filter(_scope(Incident.cluster_id, cluster))
            for audit_row, incident in query.order_by(AuditEntry.created_at.desc()).limit(MAX_ROWS_PER_SOURCE).all():
                rows.append({
                    "kind": "incident",
                    "id": audit_row.id,
                    "actor": audit_row.actor,
                    "event": audit_row.event_type,
                    "target": incident.ceph_code or incident.id,
                    "result": incident.status,
                    "created_at": _iso(audit_row.created_at),
                    "request_id": audit_row.action_id or audit_row.incident_id,
                    "details_available": True,
                })

        if not kind or kind == "backup":
            for row in session.query(BackupJob).filter(
                _scope(BackupJob.cluster_id, cluster),
            ).order_by(BackupJob.created_at.desc()).limit(MAX_ROWS_PER_SOURCE).all():
                target = "/".join(value for value in (row.pool, row.image) if value) or row.job_type
                rows.append({
                    "kind": "backup",
                    "id": row.id,
                    "actor": "worker",
                    "event": row.job_type,
                    "target": target,
                    "result": row.status,
                    "created_at": _iso(row.created_at),
                    "request_id": row.id,
                    "details_available": bool(row.error_message or row.remote_key),
                })
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return rows


def build_audit_page(cluster, *, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT, kind: str = "") -> dict:
    page_size = max(10, min(PAGE_SIZE_MAX, int(page_size)))
    page = max(1, int(page))
    if kind not in {"", "object_storage", "incident", "backup"}:
        kind = ""
    rows = _rows(cluster, kind)
    total = len(rows)
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "cluster_id": cluster.id,
        "cluster_name": cluster.name,
        "read_only": True,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "kind": kind,
        "items": rows[start:start + page_size],
    }


@router.get("/api/audit/storage")
async def storage_audit_api(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=10, le=PAGE_SIZE_MAX),
    kind: str = Query("", max_length=32),
    user: str = Depends(require_login),
):
    del user
    return build_audit_page(selected_cluster(request), page=page, page_size=page_size, kind=kind)


@router.get("/audit", response_class=HTMLResponse)
async def storage_audit_page(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=10, le=PAGE_SIZE_MAX),
    kind: str = Query("", max_length=32),
    user: str = Depends(require_login),
):
    clusters, cluster = cluster_selection(request)
    audit_page = build_audit_page(cluster, page=page, page_size=page_size, kind=kind)
    return templates.TemplateResponse(request, "storage_audit.html", {
        "user": user,
        "is_admin": False,
        "clusters": clusters,
        "selected_cluster": cluster,
        "audit_page": audit_page,
    })
