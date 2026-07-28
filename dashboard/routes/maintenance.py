import logging
import re
from collections import deque
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.settings import (
    DASHBOARD_LOG_PATH,
    WATCHER_LOG_PATH,
    WORKER_LOG_PATH,
    _settings_context,
)
from dashboard.templating import make_templates
from shared import db
from shared.models import Action, AuditEntry, ChatMessage, Incident, NodeDiagnosticRun, VolumeMetric

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

LOG_PATHS = {
    "dashboard": DASHBOARD_LOG_PATH,
    "watcher": WATCHER_LOG_PATH,
    "worker": WORKER_LOG_PATH,
}

# 2026-07-28: same "own copy, not a cross-import" posture as
# dashboard/routes/users.py's identical helper — auth.is_admin_user is the
# single source of truth either copy defers to.
def _require_admin_privilege(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )


# Every OTHER error message in this app just says "xem log server để biết
# chi tiết" (go check the server log yourself, over SSH/terminal) — this
# is the first place that log is actually reachable from the browser.
# Bounded to the last N lines (not the whole file) for the same reason
# maintenance's own log purge treats these as unbounded-growth,
# append-forever files: reading one in full on every request would only
# get slower as the deployment ages.
MAX_SERVER_LOG_LINES = 500

# Matches the "%(asctime)s ..." prefix watcher/main.py and worker/main.py's
# logging.basicConfig() now write (default asctime format: "YYYY-MM-DD
# HH:MM:SS,mmm"). dashboard.log (uvicorn's own format) never carries this —
# every line there has no parseable timestamp, see _purge_log_file's
# docstring for what that means for a date-cutoff purge.
_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _line_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _purge_log_file(path: Path, cutoff: datetime | None) -> int:
    """Returns bytes removed. `cutoff=None` clears the file entirely.

    Best-effort line-level filtering: a line with no parseable leading
    timestamp (a traceback continuation, or — for dashboard.log specifically
    — EVERY line, since uvicorn's own format carries none) is treated as
    belonging to the most recently seen timestamped line, kept/deleted
    together with it. Content before the FIRST timestamped line in the file
    has no knowable age and is always treated as older than any cutoff — it
    can only predate when timestamping started. Net effect: a date cutoff on
    dashboard.log always clears the whole file (matches reality — there is
    no date information there to filter on at all).

    Truncates the EXISTING file in place (`path.open("w")`) rather than
    replacing it (`os.replace`) — the Dashboard/Watcher/Worker processes
    hold this file open in APPEND mode for the lifetime of the process,
    keyed to the inode; replacing the inode would leave them writing into a
    now-unlinked file forever, invisible to anyone reading the path.
    """
    if not path.exists():
        return 0
    original_size = path.stat().st_size
    if cutoff is None:
        new_text = ""
    else:
        kept_lines = []
        keep_current = False
        with path.open(errors="replace") as f:
            for line in f:
                ts = _line_timestamp(line)
                if ts is not None:
                    keep_current = ts >= cutoff
                if keep_current:
                    kept_lines.append(line)
        new_text = "".join(kept_lines)
    with path.open("w") as f:
        f.write(new_text)
    return original_size - len(new_text.encode())


def _tail_log_lines(path: Path, term: str = "") -> list[str]:
    """At most the last MAX_SERVER_LOG_LINES lines of `path`, optionally
    restricted to lines containing `term` (case-insensitive substring, same
    matching posture as watcher/rgw_log.py's own filter) — filtered BEFORE
    truncating to the line cap, so a specific search term (e.g. an error
    code) still finds its match even if it scrolled past the last 500 raw
    lines. A `deque(maxlen=...)` keeps memory bounded to the cap regardless
    of how large the underlying file has grown, at the cost of still
    reading the whole file once per request — acceptable for an
    admin-only, on-demand view, not a live high-frequency poll.

    Missing file (process hasn't logged anything yet) returns an empty
    list, not an error — same "no data yet, not broken" posture as
    query_rbd_iostat's own missing-data case.
    """
    if not path.exists():
        return []
    term_lower = term.lower()
    kept: deque[str] = deque(maxlen=MAX_SERVER_LOG_LINES)
    with path.open(errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if term_lower and term_lower not in line.lower():
                continue
            kept.append(line)
    return list(kept)


def purge_old_records(cutoff: datetime | None) -> dict[str, int]:
    """`cutoff=None` deletes every row in every table below. Otherwise
    deletes Incidents (+ their Actions + AuditEntries, cascaded manually —
    there is no ORM/DB cascade configured, and Action/AuditEntry's own FKs
    would reject an orphaning delete) detected before `cutoff`, and
    NodeDiagnosticRuns/VolumeMetrics created before `cutoff` (unrelated
    tables, no FK to Incident).

    ChatMessage.proposed_incident_id also FKs to Incident (set once a chat
    proposal is confirmed, dashboard/routes/chat.py:446) but chat history
    itself is out of this purge's scope (the Settings page checkbox only
    promises Incident/Audit Trail/CLI diagnostics) — so those rows are
    de-referenced (set to NULL) rather than deleted, same
    "manually break the FK before deleting the parent" need as
    Action/AuditEntry above, just without also deleting the child.
    Skipping this step used to make the whole purge fail with a FOREIGN KEY
    constraint error the moment any old incident had ever been confirmed
    from chat.

    This is the intentional second writer to AuditEntry beyond
    shared/audit.py::record() (AD-7 documents that as the only INSERT path,
    not the only DELETE path) — an explicit, operator-triggered purge, not
    a silent background job.
    """
    with db.SessionLocal() as session:
        incident_query = session.query(Incident.id)
        if cutoff is not None:
            incident_query = incident_query.filter(Incident.detected_at < cutoff)
        incident_ids = [row[0] for row in incident_query.all()]

        audit_deleted = 0
        action_deleted = 0
        incident_deleted = 0
        if incident_ids:
            session.query(ChatMessage).filter(
                ChatMessage.proposed_incident_id.in_(incident_ids)
            ).update({"proposed_incident_id": None}, synchronize_session=False)
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

        diagnostic_query = session.query(NodeDiagnosticRun)
        if cutoff is not None:
            diagnostic_query = diagnostic_query.filter(NodeDiagnosticRun.created_at < cutoff)
        diagnostic_deleted = diagnostic_query.delete(synchronize_session=False)

        # VolumeMetric (2026-07-28): unrelated table, no FK to Incident —
        # same shape as the NodeDiagnosticRun purge above. By far the
        # fastest-growing table this purge touches (one row per volume
        # EVERY Watcher poll, not just on an Incident), so an operator
        # running this feature should expect to actually need this cutoff
        # regularly, not just occasionally.
        volume_metric_query = session.query(VolumeMetric)
        if cutoff is not None:
            volume_metric_query = volume_metric_query.filter(VolumeMetric.polled_at < cutoff)
        volume_metric_deleted = volume_metric_query.delete(synchronize_session=False)

        session.commit()

    return {
        "incidents": incident_deleted,
        "actions": action_deleted,
        "audit_entries": audit_deleted,
        "diagnostic_runs": diagnostic_deleted,
        "volume_metrics": volume_metric_deleted,
    }


def _parse_cutoff_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    return date.fromisoformat(raw)  # HTML <input type="date"> always submits YYYY-MM-DD or ""


@router.post("/settings/cleanup", response_class=HTMLResponse)
async def cleanup_submit(
    request: Request,
    user: str = Depends(require_login),
    target_files: str | None = Form(None),
    target_db: str | None = Form(None),
    cutoff_date: str = Form(""),
):
    if not target_files and not target_db:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(user, cleanup_error="Chọn ít nhất 1 loại dữ liệu cần xóa."),
        )

    try:
        cutoff_day = _parse_cutoff_date(cutoff_date)
    except ValueError:
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(user, cleanup_error="Ngày không hợp lệ."),
        )
    # Cutoff is midnight of the chosen day — "xóa dữ liệu TRƯỚC ngày X" keeps
    # everything from X 00:00 onward, deletes everything strictly earlier.
    cutoff = datetime.combine(cutoff_day, datetime.min.time()) if cutoff_day else None

    summary_parts = []
    try:
        if target_db:
            counts = purge_old_records(cutoff)
            summary_parts.append(
                f"DB: {counts['incidents']} incident, {counts['actions']} action, "
                f"{counts['audit_entries']} audit entry, {counts['diagnostic_runs']} chẩn đoán CLI, "
                f"{counts['volume_metrics']} bản ghi hiệu năng volume."
            )
        if target_files:
            freed_total = 0
            for name, path in LOG_PATHS.items():
                freed_total += _purge_log_file(path, cutoff)
            summary_parts.append(f"File log: giải phóng {freed_total / 1024:.1f} KB.")
    except Exception:
        logger.exception("cleanup_submit: failed to purge old data")
        return templates.TemplateResponse(
            request,
            "settings.html",
            _settings_context(
                user, cleanup_error="Xóa thất bại giữa chừng — xem log server để biết chi tiết."
            ),
        )

    return templates.TemplateResponse(
        request,
        "settings.html",
        _settings_context(user, cleanup_success="Đã xóa xong. " + " ".join(summary_parts)),
    )


@router.get("/api/settings/server-log")
async def server_log_api(name: str, filter: str = "", user: str = Depends(require_login)):
    """Backs the Settings page's "Log Server" panel — reads the Dashboard/
    Watcher/Worker process log straight from the browser instead of
    requiring SSH/terminal access to the box the Dashboard runs on (every
    OTHER failure message in this app just says "xem log server để biết
    chi tiết" without actually offering a way to see it). Admin-only,
    unlike the Nodes page's read-only CLI diagnostics (any logged-in user
    can run those): a full process log can contain more than an admin
    would want a regular operator to see (full tracebacks, SSH command
    lines, internal paths).
    """
    _require_admin_privilege(user)
    if name not in LOG_PATHS:
        raise HTTPException(status_code=404, detail="Không rõ log nào được yêu cầu")
    lines = _tail_log_lines(LOG_PATHS[name], filter)
    return {"name": name, "filter": filter, "lines": lines}
