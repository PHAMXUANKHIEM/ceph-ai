"""Operator-facing, evidence-backed remediation runbook generation."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from dashboard.cluster_scope import cluster_selection
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db, remediation_runbook
from shared.models import Cluster

router = APIRouter()
templates = make_templates()


def _context(request: Request, user: str, *, clusters, selected_cluster, fault_family="",
             families=None, source=None, report=None, error="", cached=False):
    return {
        "user": user, "is_admin": auth.is_admin_user(user), "clusters": clusters,
        "selected_cluster": selected_cluster, "fault_family": fault_family,
        "fault_families": families or [], "source": source, "report": report,
        "report_cached": cached, "error": error,
    }


@router.get("/runbooks", response_class=HTMLResponse)
async def runbooks_page(request: Request, user: str = Depends(require_login)):
    clusters, selected = cluster_selection(request)
    family = request.query_params.get("fault_family", "").strip()
    report = None
    cached = False
    with db.SessionLocal() as session:
        families = remediation_runbook.list_fault_families(session, cluster_id=selected.id)
        source = None
        error = ""
        if family:
            try:
                source = remediation_runbook.build_source(
                    session, fault_family=family, cluster_id=selected.id,
                )
                report = remediation_runbook.get_cached(session, source)
                cached = report is not None
            except remediation_runbook.RunbookError as exc:
                error = str(exc)
    return templates.TemplateResponse(
        request, "runbooks.html",
        _context(request, user, clusters=clusters, selected_cluster=selected,
                 fault_family=family, families=families, source=source, report=report,
                 cached=cached, error=error),
    )


@router.post("/runbooks/generate", response_class=HTMLResponse)
async def generate_runbook(
    request: Request,
    cluster_id: str = Form(...),
    fault_family: str = Form(...),
    user: str = Depends(require_login),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được tạo runbook bằng AI")
    clusters, selected = cluster_selection(request)
    selected = next((cluster for cluster in clusters if cluster.id == cluster_id), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy cluster đang hoạt động")
    with db.SessionLocal() as session:
        families = remediation_runbook.list_fault_families(session, cluster_id=selected.id)
        try:
            source = remediation_runbook.build_source(
                session, fault_family=fault_family, cluster_id=selected.id,
            )
            report = remediation_runbook.get_cached(session, source)
        except remediation_runbook.RunbookError as exc:
            return templates.TemplateResponse(
                request, "runbooks.html",
                _context(request, user, clusters=clusters, selected_cluster=selected,
                         fault_family=fault_family, families=families, error=str(exc)),
            )
    if report is not None:
        return templates.TemplateResponse(
            request, "runbooks.html",
            _context(request, user, clusters=clusters, selected_cluster=selected,
                     fault_family=fault_family, families=families, source=source,
                     report=report, cached=True),
        )
    try:
        report = await remediation_runbook.generate(source)
    except remediation_runbook.RunbookError as exc:
        return templates.TemplateResponse(
            request, "runbooks.html",
            _context(request, user, clusters=clusters, selected_cluster=selected,
                     fault_family=fault_family, families=families, source=source, error=str(exc)),
        )
    with db.SessionLocal() as session:
        remediation_runbook.store_cached(session, source, report)
    return templates.TemplateResponse(
        request, "runbooks.html",
        _context(request, user, clusters=clusters, selected_cluster=selected,
                 fault_family=fault_family, families=families, source=source, report=report),
    )


@router.post("/runbooks/markdown", response_class=PlainTextResponse)
async def runbook_markdown(
    request: Request,
    cluster_id: str = Form(...),
    fault_family: str = Form(...),
    user: str = Depends(require_login),
):
    """Return the cached validated report or generate it once as Markdown."""
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được tải runbook bằng AI")
    try:
        with db.SessionLocal() as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None or not cluster.is_active:
                raise HTTPException(status_code=404, detail="Không tìm thấy cluster đang hoạt động")
            source = remediation_runbook.build_source(session, fault_family=fault_family, cluster_id=cluster_id)
            report = remediation_runbook.get_cached(session, source)
        if report is None:
            report = await remediation_runbook.generate(source)
            with db.SessionLocal() as session:
                remediation_runbook.store_cached(session, source, report)
    except remediation_runbook.RunbookError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", fault_family.strip()) or "runbook"
    return PlainTextResponse(
        remediation_runbook.to_markdown(report),
        headers={"Content-Disposition": f'attachment; filename="{filename}.md"'},
    )
