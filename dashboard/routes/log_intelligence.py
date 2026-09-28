"""Log Intelligence L4 (Plan/log-intelligence-rca-plan.md) -- trang xem kết
quả của cả chuỗi L0->L3.

Ba khối, xếp theo đúng thứ tự một người điều tra cần đọc:

1. **Trạng thái thu thập** -- có đang lấy được log không, có node nào hụt
   không (PARTIAL). Đặt TRÊN CÙNG có chủ ý: mọi kết luận bên dưới chỉ đáng
   tin bằng đúng độ đầy đủ của dữ liệu sinh ra nó, nên người đọc phải thấy
   điều đó trước khi đọc kết luận.
2. **Phát hiện** -- kết luận của AI, LUÔN kèm bằng chứng gốc (mẫu log thật)
   và ghi chú nếu server đã phải sửa/hạ cấp câu trả lời của model.
3. **Mẫu log** -- nơi operator gắn nhãn `BENIGN` cho nhiễu. Đây là cách
   "dạy" tầng triage im lặng mà không cần sửa code, nên nó phải nằm ngay
   trên giao diện chứ không phải trong file cấu hình.

Trang này CHỈ ĐỌC + gắn nhãn. Không có nút nào ở đây chạy lệnh ra cụm: đề
xuất hành động đi qua đúng hàng chờ Duyệt sẵn có (xem
`watcher/log_analysis.py::_maybe_propose_action`), không có đường tắt --
ràng buộc R5 của plan.
"""

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import (
    Incident,
    LogFinding,
    LogFindingStatus,
    LogIngestRun,
    LogLearningSample,
    LogFaultStat,
    LogPattern,
    LogPatternTriageLabel,
)
from watcher.log_analysis import ceph_code_for, resolve_pattern_templates

router = APIRouter()
templates = make_templates()

RUNS_PAGE_SIZE = 8
LEARNING_PAGE_SIZE = 10
RGW_PAGE_SIZE = 8
FINDINGS_PAGE_SIZE = 8
PATTERNS_PAGE_SIZE = 20


def _require_admin_privilege(user: str) -> None:
    """Gắn nhãn `BENIGN` là thao tác làm hệ thống IM LẶNG với một loại log —
    tức là một quyết định an toàn, không phải tuỳ chọn hiển thị. Giữ ở mức
    admin, cùng posture với /capability-matrix."""
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )


def _safe_page(value: int) -> int:
    return max(1, min(int(value), 10_000))


def _paginate(query, page: int, page_size: int):
    total = query.order_by(None).count()
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(_safe_page(page), pages)
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return rows, {"page": page, "pages": pages, "total": total, "page_size": page_size}


def _context(
    user: str,
    *,
    runs_page: int = 1,
    learning_page: int = 1,
    rgw_page: int = 1,
    findings_page: int = 1,
    patterns_page: int = 1,
    message: str | None = None,
    error: str | None = None,
) -> dict:
    with db.SessionLocal() as session:
        runs, runs_pagination = _paginate(
            session.query(LogIngestRun).order_by(LogIngestRun.created_at.desc()),
            runs_page, RUNS_PAGE_SIZE,
        )
        findings_query = session.query(LogFinding).order_by(LogFinding.created_at.desc())
        findings, findings_pagination = _paginate(findings_query, findings_page, FINDINGS_PAGE_SIZE)
        patterns, patterns_pagination = _paginate(
            session.query(LogPattern).order_by(LogPattern.last_seen_at.desc()),
            patterns_page, PATTERNS_PAGE_SIZE,
        )
        learning_samples, learning_pagination = _paginate(
            session.query(LogLearningSample).order_by(LogLearningSample.updated_at.desc()),
            learning_page, LEARNING_PAGE_SIZE,
        )
        rgw_findings, rgw_pagination = _paginate(
            session.query(LogFinding).filter(
                LogFinding.affected_daemons_json.like('%"rgw"%')
            ).order_by(LogFinding.created_at.desc()),
            rgw_page, RGW_PAGE_SIZE,
        )
        run_status_counts = {
            status: session.query(LogIngestRun).filter(LogIngestRun.status == status).count()
            for status in ("OK", "PARTIAL", "FAILED")
        }
        learning_stats = session.query(LogFaultStat).all()
        correlated_ids = {finding.correlated_incident_id for finding in findings if finding.correlated_incident_id}
        correlated_incidents = {
            incident.id: incident
            for incident in session.query(Incident).filter(Incident.id.in_(correlated_ids)).all()
        } if correlated_ids else {}

        finding_rows = []
        for finding in findings:
            try:
                daemon_types = {
                    value.lower() for value in json.loads(finding.affected_daemons_json or "[]")
                    if isinstance(value, str)
                }
            except (TypeError, ValueError):
                daemon_types = set()
            finding_rows.append({
                "row": finding,
                # Bằng chứng gốc luôn đi kèm kết luận -- người đọc phải tự
                # đánh giá được, không phải tin lời model.
                "evidence": resolve_pattern_templates(finding),
                "ceph_code": ceph_code_for(finding.dedupe_key),
                "correlated_incident": correlated_incidents.get(finding.correlated_incident_id),
                "is_rgw": "rgw" in daemon_types,
            })

        learning_rows = []
        for sample in learning_samples:
            stat = next(
                (
                    row for row in learning_stats
                    if row.cluster_id == sample.cluster_id
                    and row.daemon_type == sample.daemon_type
                    and row.fault_family == sample.fault_family
                    and row.playbook_id == (sample.recommended_playbook_id or "observation_only")
                ),
                None,
            )
            learning_rows.append({
                "row": sample,
                "stat": stat,
                "gate": "audit-only · shadow · commission not enabled",
            })

        # Tách các đối tượng ra khỏi session trước khi nó đóng: template
        # chỉ đọc thuộc tính đã nạp, không lazy-load thêm.
        session.expunge_all()

    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "runs": runs,
        "runs_pagination": runs_pagination,
        "run_status_counts": run_status_counts,
        "learning_rows": learning_rows,
        "learning_pagination": learning_pagination,
        "finding_rows": finding_rows,
        "rgw_finding_rows": [{"row": row, "is_rgw": True} for row in rgw_findings],
        "rgw_pagination": rgw_pagination,
        "findings_pagination": findings_pagination,
        "patterns": patterns,
        "patterns_pagination": patterns_pagination,
        "summary": {
            "findings": findings_query.order_by(None).count(),
            "open_findings": session.query(LogFinding).filter(
                LogFinding.status == LogFindingStatus.OPEN.value
            ).count(),
            "unknown_patterns": session.query(LogPattern).filter(
                LogPattern.triage_label == LogPatternTriageLabel.UNKNOWN.value
            ).count(),
        },
        "open_status": LogFindingStatus.OPEN.value,
        "acknowledged_status": LogFindingStatus.ACKNOWLEDGED.value,
        "resolved_status": LogFindingStatus.RESOLVED.value,
        "message": message,
        "error": error,
    }


@router.get("/log-intelligence", response_class=HTMLResponse)
async def log_intelligence_page(
    request: Request,
    runs_page: int = Query(1, ge=1),
    learning_page: int = Query(1, ge=1),
    rgw_page: int = Query(1, ge=1),
    findings_page: int = Query(1, ge=1),
    patterns_page: int = Query(1, ge=1),
    user: str = Depends(require_login),
):
    return templates.TemplateResponse(
        request,
        "log_intelligence.html",
        _context(
            user,
            runs_page=runs_page,
            learning_page=learning_page,
            rgw_page=rgw_page,
            findings_page=findings_page,
            patterns_page=patterns_page,
        ),
    )


@router.post("/log-intelligence/findings/{finding_id}/acknowledge")
async def acknowledge_finding(finding_id: str, user: str = Depends(require_login)):
    """OPEN -> ACKNOWLEDGED: operator đã đọc và đang xử lý.

    KHÔNG chuyển thẳng sang RESOLVED: một phát hiện chỉ được coi là hết khi
    các mẫu log của nó thật sự ngừng xuất hiện, và đó là việc của
    `watcher/log_analysis.py::resolve_stale_findings` đo bằng dữ liệu, không
    phải việc của một cú bấm nút. Nút này chỉ nói "tôi đã thấy rồi", để
    người khác trong ca trực không phải điều tra lại từ đầu."""
    with db.SessionLocal() as session:
        finding = session.get(LogFinding, finding_id)
        if finding is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy phát hiện này")
        if finding.status == LogFindingStatus.OPEN.value:
            finding.status = LogFindingStatus.ACKNOWLEDGED.value
            session.commit()
    return RedirectResponse("/log-intelligence", status_code=303)

@router.post("/log-intelligence/patterns/bulk-label")
async def bulk_label_patterns(
    pattern_ids: list[str] = Form(default=[]),
    label: str = Form(""),
    user: str = Depends(require_login),
):
    """Apply one triage label to selected patterns in a single admin action."""
    _require_admin_privilege(user)
    valid = {item.value for item in LogPatternTriageLabel}
    if label not in valid or not pattern_ids:
        return RedirectResponse("/log-intelligence?error=bulk-label", status_code=303)
    with db.SessionLocal() as session:
        rows = session.query(LogPattern).filter(LogPattern.id.in_(pattern_ids)).all()
        for pattern in rows:
            pattern.triage_label = label
        session.commit()
    return RedirectResponse("/log-intelligence", status_code=303)


@router.post("/log-intelligence/patterns/{pattern_id}/label")
async def label_pattern(
    pattern_id: str,
    user: str = Depends(require_login),
    label: str = Form(""),
):
    """Gắn `BENIGN`/`NOTABLE`/`UNKNOWN` cho một mẫu log.

    `BENIGN` là cách operator tắt nhiễu vĩnh viễn cho một loại dòng log mà
    không cần sửa code hay đổi ngưỡng — tầng triage (L1) loại bỏ mẫu này
    trước mọi kiểm tra khác. Vì thế nó bị giới hạn ở admin: dùng sai sẽ làm
    hệ thống im lặng với đúng thứ lẽ ra phải báo."""
    _require_admin_privilege(user)

    valid = {item.value for item in LogPatternTriageLabel}
    if label not in valid:
        return RedirectResponse(
            "/log-intelligence?error=label", status_code=303
        )

    with db.SessionLocal() as session:
        pattern = session.get(LogPattern, pattern_id)
        if pattern is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy mẫu log này")
        pattern.triage_label = label
        session.commit()
    return RedirectResponse("/log-intelligence", status_code=303)
