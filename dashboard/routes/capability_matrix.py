"""AI roadmap Pha 0.2 (Plan/ai-missing-features-roadmap.md) -- admin-only
CRUD page over `shared/capability_matrix.py`. Deliberately NOT a page that
lets an operator paste free-text "AI said this is supported" -- every
field maps directly to a `CapabilityMatrixEntry` column, and `verified_by`
is always the logged-in admin's own username (never a free-text field),
so every entry created here is honestly attributable to whoever actually
checked the doc_url before adding it (roadmap section 3.2's "không dùng
blog hoặc câu trả lời cộng đồng làm nguồn quyết định" -- the burden of
checking a real source is on the human filling this form, not on the
page itself).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from config.settings import settings
from dashboard.templating import make_templates
from shared import capability_matrix, capability_seed, db
from shared.clusters import list_active_clusters
from watcher import capability_inventory

router = APIRouter()
templates = make_templates()


def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )


def _coverage_by_cluster() -> list[dict]:
    """Độ phủ matrix cho TỪNG cụm đang hoạt động, tính theo đúng phiên bản
    Ceph mà Pha 0.1 dò được của cụm đó.

    Tính theo cụm chứ không phải một con số chung, vì một entry chỉ phủ một
    khoảng major version: cùng bảng matrix có thể đã đủ cho cụm Reef nhưng
    vẫn hổng cho cụm Nautilus bên cạnh. Gộp lại thành một chỉ số duy nhất
    sẽ giấu mất đúng cái hổng đó.
    """
    result = []
    with db.SessionLocal() as session:
        for cluster in list_active_clusters(session):
            snapshot = capability_inventory.latest_snapshot(cluster.id, session=session)
            ceph_major = snapshot.current_major if snapshot is not None else None
            report = capability_matrix.coverage_report(ceph_major, session=session)
            report["cluster_name"] = cluster.name
            report["scanned"] = snapshot is not None
            result.append(report)
    return result


def _context(
    user: str,
    *,
    create_error: str | None = None,
    create_success: str | None = None,
    deprecate_error: str | None = None,
    deprecate_success: str | None = None,
    form_values: dict | None = None,
) -> dict:
    entries = capability_matrix.list_entries(include_deprecated=True)
    changes_by_entry = {e.id: capability_matrix.list_changes(e.id) for e in entries}
    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "entries": entries,
        "proposals": capability_seed.list_proposals(),
        "coverage": _coverage_by_cluster(),
        "enforcement_enabled": settings.ai_preflight_enforcement_enabled,
        "changes_by_entry": changes_by_entry,
        "create_error": create_error,
        "create_success": create_success,
        "deprecate_error": deprecate_error,
        "deprecate_success": deprecate_success,
        "form_values": form_values or {},
    }


@router.post("/capability-matrix/ai-propose", response_class=HTMLResponse)
async def ai_propose_capabilities(request: Request, user: str = Depends(require_login), doc_url: str = Form(""), release_notes: str = Form("")):
    _require_admin_privilege(user)
    try:
        rows = await capability_seed.generate(doc_url=doc_url, release_notes=release_notes, actor=user)
        message = f"AI đã tạo {len(rows)} bản nháp chờ duyệt."
        return templates.TemplateResponse(request, "capability_matrix.html", _context(user, create_success=message))
    except Exception as exc:
        return templates.TemplateResponse(request, "capability_matrix.html", _context(user, create_error=str(exc)))


@router.post("/capability-matrix/proposals/{proposal_id}/{decision}", response_class=HTMLResponse)
async def review_capability_proposal(proposal_id: str, decision: str, request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    if decision not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Quyết định không hợp lệ")
    row = capability_seed.review(proposal_id, approve=decision == "approve", actor=user)
    if row is None:
        raise HTTPException(status_code=409, detail="Proposal không tồn tại hoặc đã được review")
    return templates.TemplateResponse(request, "capability_matrix.html", _context(user, create_success=f"Đã {decision} proposal {row.command_id}."))


@router.get("/capability-matrix", response_class=HTMLResponse)
async def capability_matrix_page(request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    return templates.TemplateResponse(request, "capability_matrix.html", _context(user))


@router.post("/capability-matrix/create", response_class=HTMLResponse)
async def create_capability_matrix_entry(
    request: Request,
    user: str = Depends(require_login),
    command_id: str = Form(""),
    inner_command: str = Form(""),
    doc_url: str = Form(""),
    min_major: str = Form(""),
    max_major: str = Form(""),
    flag: str = Form(""),
    module: str = Form(""),
    backend: str = Form(""),
    notes: str = Form(""),
):
    """Adds a new ACTIVE entry -- `verified_by` is always the logged-in
    admin (never a value from the form), so the audit trail can never be
    forged by whoever fills the textarea."""
    _require_admin_privilege(user)

    submitted = {
        "command_id": command_id, "inner_command": inner_command, "doc_url": doc_url,
        "min_major": min_major, "max_major": max_major, "flag": flag, "module": module,
        "backend": backend, "notes": notes,
    }

    error: str | None = None
    command_id = command_id.strip()
    inner_command = inner_command.strip()
    doc_url = doc_url.strip()
    if not command_id or not inner_command or not doc_url:
        error = "Vui lòng điền Command ID, Inner command và Doc URL."
    elif not (doc_url.startswith("https://") or doc_url.startswith("http://")):
        error = "Doc URL phải là một đường link thật (http/https) tới tài liệu Ceph chính thức."
    min_major_value: int | None = None
    max_major_value: int | None = None
    if error is None:
        try:
            min_major_value = int(min_major)
        except ValueError:
            error = "Min major version phải là số nguyên."
    if error is None and max_major.strip():
        try:
            max_major_value = int(max_major)
        except ValueError:
            error = "Max major version phải là số nguyên hoặc để trống."
    if error is None and max_major_value is not None and max_major_value < min_major_value:
        error = "Max major version phải lớn hơn hoặc bằng Min major version."

    if error:
        return templates.TemplateResponse(
            request, "capability_matrix.html",
            _context(user, create_error=error, form_values=submitted),
        )

    capability_matrix.create_entry(
        command_id=command_id,
        inner_command=inner_command,
        doc_url=doc_url,
        verified_by=user,
        verified_at=datetime.utcnow(),
        min_major=min_major_value,
        max_major=max_major_value,
        flag=flag.strip() or None,
        module=module.strip() or None,
        backend=backend.strip() or None,
        notes=notes.strip() or None,
    )
    return templates.TemplateResponse(
        request, "capability_matrix.html",
        _context(user, create_success=f"Đã thêm entry cho {command_id!r}."),
    )


@router.post("/capability-matrix/{entry_id}/deprecate", response_class=HTMLResponse)
async def deprecate_capability_matrix_entry(
    entry_id: str, request: Request, user: str = Depends(require_login)
):
    _require_admin_privilege(user)
    entry = capability_matrix.deprecate_entry(entry_id, actor=user)
    if entry is None:
        return templates.TemplateResponse(
            request, "capability_matrix.html",
            _context(user, deprecate_error="Không tìm thấy entry."),
        )
    return templates.TemplateResponse(
        request, "capability_matrix.html",
        _context(user, deprecate_success=f"Đã deprecate entry cho {entry.command_id!r}."),
    )
