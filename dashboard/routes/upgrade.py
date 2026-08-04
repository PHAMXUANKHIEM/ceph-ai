import asyncio
import json
import logging
import re
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from openai import APIError, APIConnectionError, AuthenticationError
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from dashboard.vntime import format_vn
from shared import audit, db
from shared.ceph_releases import codename_for_version, codenames_oldest_first, versions_by_codename
from shared.cluster_nodes import configured_nodes
from shared.models import Action, ActionStatus, Incident, IncidentStatus, UpgradeProcedureDocument
from shared.router_client import RouterNotConfiguredError, build_router_client, readable_exception_message
from watcher.ceph_client import (
    CephQueryError,
    get_upgrade_status,
    pause_upgrade,
    propose_next_version,
    resume_upgrade,
    run_ceph_json_command,
    summarize_cluster_versions,
    unset_upgrade_osd_flags,
)
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# Synthetic Incident.ceph_code for this feature — same trick
# dashboard/routes/chat.py uses (CHAT_REQUEST_CEPH_CODE): AuditEntry.incident_id
# is a required FK (AD-7), and this feature has no real detected Incident
# behind it, only an operator explicitly proposing an upgrade. Reusing the
# existing Action/Incident/audit/kill-switch/approval pipeline (Epic 3/4)
# this way means dashboard/routes/actions.py's approve/reject routes and
# worker/llm/router_client.py's approved-action poller need ZERO changes to
# support this feature.
CLUSTER_UPGRADE_CEPH_CODE = "CLUSTER_UPGRADE"

# Three action_id "flavors", one per supported deployment style — see
# worker/policy/action_policy.yaml's `cluster_upgrade_action_ids:` comment.
CLUSTER_UPGRADE_ACTION_ID = "upgrade_ceph_cluster"  # cephadm
PACKAGE_DOWNLOAD_ACTION_ID = "upgrade_ceph_cluster_package_download"  # ceph-deploy, download.ceph.com
PACKAGE_LOCAL_ACTION_ID = "upgrade_ceph_cluster_package_local"  # ceph-deploy, local package dir
CLUSTER_UPGRADE_ACTION_IDS = frozenset(
    {CLUSTER_UPGRADE_ACTION_ID, PACKAGE_DOWNLOAD_ACTION_ID, PACKAGE_LOCAL_ACTION_ID}
)

_TARGET_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PACKAGE_DIR_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)

# Story 7.2 (2026-08-04): "chạy bộ test sau nâng cấp" checkbox — package-
# based propose forms only (see propose_package_download_upgrade/
# propose_package_local_upgrade below), never the cephadm one. Purely a
# flag + a link out to the SEPARATE Epic 10 React app
# (ceph-aiops/ceph-upgrade-test-runner-frontend/, not yet wired to any real
# backend test-execution call — Stories 10.3-10.7) — this app never makes
# an HTTP call to it. Dev server URL per that app's own vite.config.js
# (`npm run dev`, port 5173) — a DEFAULT/FALLBACK only, used when
# settings.test_runner_frontend_url is unset (code review fix, 2026-08-04:
# this used to be hardcoded with no way to override it, which is broken by
# construction whenever the operator's browser isn't on the same machine as
# this Dashboard backend). An operator running a real build sets
# settings.test_runner_frontend_url (.env/Settings page) to wherever
# they've actually deployed it.
TEST_RUNNER_FRONTEND_URL = "http://localhost:5173"
RUN_TEST_SUITE_PARAM_KEY = "run_test_suite"

# Story 7.2: `Action.execution_progress` entries gained a `phase` key
# (install/mon/mgr/osd/mds_rgw — see worker/llm/router_client.py's
# _UPGRADE_PHASE_* constants). Entries written before this story shipped
# (and the flags/finalize steps, which are deliberately NOT phase-tagged —
# see worker/llm/router_client.py::_set_upgrade_osd_flags' own comment on
# why) have no `phase` key at all; render those with this fallback label
# rather than crashing or showing a blank phase.
_PHASE_LABELS = {
    "install": "Cài đặt",
    "mon": "Khởi động lại MON",
    "mgr": "Khởi động lại MGR",
    "osd": "Khởi động lại OSD",
    "mds_rgw": "Khởi động lại MDS/RGW",
}
_FALLBACK_PHASE_LABEL = "Cài đặt"


def _phase_label(step: dict) -> str:
    return _PHASE_LABELS.get(step.get("phase"), _FALLBACK_PHASE_LABEL)


# Same 3 labels upgrade.html's method display already hardcodes per
# action_id — factored out here too so the generated markdown log (below)
# uses the exact same wording instead of drifting from the page.
_ACTION_ID_LABELS = {
    CLUSTER_UPGRADE_ACTION_ID: "cephadm (ceph orch upgrade)",
    PACKAGE_DOWNLOAD_ACTION_ID: "ceph-deploy — tải từ download.ceph.com",
    PACKAGE_LOCAL_ACTION_ID: "ceph-deploy — gói cục bộ trên node",
}

_ACTION_STATUS_LABELS = {
    ActionStatus.PENDING_APPROVAL.value: "Chờ duyệt",
    ActionStatus.APPROVED.value: "Đã duyệt — đang chờ Worker thực thi",
    ActionStatus.EXECUTED.value: "Thành công",
    ActionStatus.FAILED.value: "Thất bại",
    ActionStatus.REJECTED.value: "Đã từ chối",
}

_STEP_STATUS_LABELS = {
    "pending": "Đang chờ",
    "running": "Đang chạy",
    "done": "✅ Xong",
    "failed": "❌ Lỗi",
    "skipped": "⏭️ Bỏ qua",
}

_CEPHADM_PLAN_TEMPLATE = """\
Lệnh sẽ gửi tới cụm: `ceph orch upgrade start --ceph-version {target_version}`

cephadm sẽ tự động điều phối toàn bộ quá trình nâng cấp lên {target_version}:
1. Kiểm tra cụm đang HEALTH_OK trước khi bắt đầu — nếu cụm đang WARN/ERR, cephadm mặc định
   dừng lại, không tự nâng cấp lên một cụm đang có vấn đề.
2. Nâng cấp lần lượt từng nhóm daemon, mỗi daemon một lần: mgr (standby trước, active sau)
   → mon → crash → osd → mds → rgw → rbd-mirror → ceph-exporter. Sau mỗi daemon, cephadm chờ
   daemon đó healthy trở lại rồi mới chuyển sang daemon tiếp theo — không nâng cấp đồng thời
   nhiều daemon cùng lúc.
3. Toàn bộ quá trình chạy nền bên trong mgr của cephadm, độc lập với tiến trình Worker của hệ
   thống này — có thể theo dõi tiến độ trực tiếp ngay tại trang này (mục "Tiến độ nâng cấp").
4. Nếu cụm chuyển sang HEALTH_ERR giữa chừng, cephadm tự tạm dừng (không tự rollback) — cần
   theo dõi và can thiệp thủ công nếu xảy ra. Bạn cũng có thể chủ động bấm "Tạm dừng" bất cứ
   lúc nào trong lúc nâng cấp đang chạy.

An toàn trước khi thực thi: hệ thống sẽ kiểm tra lại cờ kill-switch ngay trước khi gửi lệnh
`ceph orch upgrade start` ở trên (nếu kill-switch đang bật, lệnh sẽ không được gửi và đề xuất
quay lại trạng thái chờ duyệt). Lưu ý: kill-switch chỉ chặn được việc GỬI lệnh bắt đầu — một
khi cephadm đã bắt đầu điều phối nâng cấp, việc dừng lại giữa chừng cần bấm "Tạm dừng" ở trên,
không phải kill-switch (xem watcher/ceph_client.py để biết lý do)."""

# 2026-07-23: shared tail for both package-based (ceph-deploy) plan texts
# below — the execution-model caveat that does NOT apply to the cephadm
# plan above (there IS an orchestrator gating cephadm's steps on cluster
# health; there is none here — see worker/executor/commands.py's module
# comment on the package-based builders for the full reasoning).
_PACKAGE_METHOD_SAFETY_NOTE = """\
QUAN TRỌNG — khác với cephadm: KHÔNG có orchestrator tự kiểm tra sức khoẻ cụm giữa các bước.
Nếu một bước gặp lỗi, hệ thống VẪN thử tiếp bước kế tiếp (không tự dừng lại) — cách duy nhất để
dừng giữa chừng là bấm nút khẩn cấp (kill-switch) trên Dashboard NGAY khi phát hiện sự cố:
kill-switch được kiểm tra lại trước MỖI bước (từng node, trong từng giai đoạn) — bước tiếp theo
sẽ không được thử nếu kill-switch đang bật; các bước chưa chạy sẽ được ghi "Bỏ qua". Khuyến nghị
theo dõi sát Audit Trail trong suốt quá trình, đặc biệt với cụm nhiều node.

An toàn trước khi thực thi: hệ thống sẽ kiểm tra lại cờ kill-switch ngay trước khi chạy bước
đầu tiên (nếu đang bật, đề xuất quay lại trạng thái chờ duyệt, chưa node nào bị đụng tới)."""


def _upgrade_plan_text(target_version: str) -> str:
    return _CEPHADM_PLAN_TEMPLATE.format(target_version=target_version)


# Story 7.2 (2026-08-04): both package-based plan texts below now describe
# the PHASED execution worker/llm/router_client.py::_execute_package_upgrade_action
# actually runs — install everywhere first, then restart strictly
# MON -> MGR -> OSD (role-scoped, one daemon type at a time) across the
# WHOLE cluster, then any remaining host with a leftover MDS/RGW unit —
# instead of the old "install-then-restart-everything, one host at a
# time" description, which no longer matches what gets sent.
_PACKAGE_PHASE_STEPS = """\
2. **Khởi động lại MON:** trên các node có vai trò MON, chỉ khởi động lại (các) unit MON của
   node đó.
3. **Khởi động lại MGR:** trên các node có vai trò MGR, chỉ khởi động lại (các) unit MGR.
4. **Khởi động lại OSD:** trên các node có vai trò OSD, chỉ khởi động lại (các) unit OSD.
5. **Khởi động lại MDS/RGW còn lại:** node nào còn unit MDS/RGW (dò qua `systemctl`) chưa được
   khởi động lại ở các bước trên thì khởi động lại ở bước cuối này.

Một node đảm nhiều vai trò (vd MON+OSD) chỉ CÀI ĐẶT MỘT LẦN ở bước 1, nhưng được KHỞI ĐỘNG LẠI
RIÊNG cho từng vai trò, đúng vào giai đoạn tương ứng của vai trò đó — không bao giờ khởi động
lại cùng một unit hai lần, không bao giờ bỏ sót vai trò nào."""


def _package_download_plan_text(target_version: str, codename: str, target_nodes: list[str]) -> str:
    return (
        f"Cụm này dùng kiểu triển khai ceph-deploy/gói cài đặt trực tiếp "
        f"(ceph_exec_mode=none) — hệ thống sẽ tự thực hiện tuần tự 5 giai đoạn sau trên "
        f"{len(target_nodes)} node đã cấu hình (thứ tự cài đặt: {', '.join(target_nodes)}):\n"
        f"1. **Cài đặt (mọi node):** thêm/cập nhật repo gói Ceph chính thức từ "
        f"download.ceph.com cho bản {target_version} (mã tên release: {codename}) rồi cài/nâng "
        f"cấp gói `ceph` — tự nhận diện apt (Debian/Ubuntu) hay yum/dnf (RHEL/CentOS) — trên "
        f"TẤT CẢ các node trên, KHÔNG khởi động lại daemon nào ở bước này.\n"
        f"{_PACKAGE_PHASE_STEPS}\n\n"
        f"{_PACKAGE_METHOD_SAFETY_NOTE}"
    )


def _package_local_plan_text(package_dir: str, target_nodes: list[str]) -> str:
    return (
        f"Cụm này dùng kiểu triển khai ceph-deploy/gói cài đặt trực tiếp "
        f"(ceph_exec_mode=none) — hệ thống sẽ tự thực hiện tuần tự 5 giai đoạn sau trên "
        f"{len(target_nodes)} node đã cấu hình (thứ tự cài đặt: {', '.join(target_nodes)}):\n"
        f"1. **Cài đặt (mọi node):** kiểm tra thư mục `{package_dir}` tồn tại rồi cài các gói "
        f".deb/.rpm có sẵn TRONG THƯ MỤC NÀY (không tải gì từ Internet — gói phải đã được đặt "
        f"sẵn tại CÙNG đường dẫn `{package_dir}` trên MỌI node liên quan từ trước) trên TẤT CẢ "
        f"các node trên, KHÔNG khởi động lại daemon nào ở bước này.\n"
        f"{_PACKAGE_PHASE_STEPS}\n\n"
        f"{_PACKAGE_METHOD_SAFETY_NOTE}"
    )


# --- Uploaded upgrade-procedure document (operator's own runbook, AI-
# summarized) -----------------------------------------------------------

ALLOWED_PROCEDURE_EXTENSIONS = (".txt", ".md")
# No PDF/Word parser dependency in this codebase — only plain-text formats
# are accepted; a binary/undecodable upload is rejected with a clear error
# rather than silently mangling it (see upload_upgrade_procedure below).
MAX_PROCEDURE_FILE_BYTES = 2 * 1024 * 1024  # 2 MB — generous for a text runbook
# 9router request-size guard, same posture as chat_client.py's
# MAX_TOOL_RESULT_CHARS — the full document is always kept in
# UpgradeProcedureDocument.raw_text regardless; only what's SENT to the
# model for summarization is capped.
MAX_PROCEDURE_TEXT_CHARS_FOR_AI = 40_000
PROCEDURE_SUMMARY_MAX_TOKENS = 2048
PROCEDURE_SUMMARY_ROUTER_TIMEOUT_SECONDS = 60.0

_PROCEDURE_SUMMARY_SYSTEM_PROMPT = (
    "Bạn là trợ lý vận hành cụm Ceph. Operator vừa upload một tài liệu quy trình "
    "nâng cấp cụm do chính họ soạn (runbook nội bộ, không phải tài liệu chính thức "
    "của Ceph). Đọc kỹ toàn bộ nội dung và tóm tắt lại thành các bước rõ ràng, đánh "
    "số thứ tự, ngắn gọn, dễ làm theo, bằng tiếng Việt. GIỮ NGUYÊN mọi cảnh báo, "
    "điều kiện tiên quyết, và lệnh cụ thể (nếu có) trong tài liệu gốc — không được "
    "bỏ sót thông tin an toàn quan trọng. Nếu tài liệu không liên quan gì đến nâng "
    "cấp cụm Ceph, hãy nói rõ điều đó thay vì bịa ra một quy trình."
)


class UpgradeProcedureSummaryError(Exception):
    """Raised when 9router itself fails while summarizing an uploaded
    procedure document (not configured, auth, network, empty response) —
    caught by upload_upgrade_procedure/resummarize_upgrade_procedure below
    and stored as UpgradeProcedureDocument.summary_error rather than failing
    the upload/retry outright. The raw document is always saved regardless
    of whether summarization succeeds — 9router being unreachable or
    unconfigured must not lose the operator's uploaded runbook."""


async def _summarize_upgrade_procedure(raw_text: str) -> str:
    """Sends the operator's uploaded upgrade-procedure document to 9router
    for a plain-language Vietnamese summary — same client + streaming-call
    pattern dashboard/chat_client.py and worker/llm/router_client.py already
    use against this router (verified: it always responds via SSE regardless
    of whether streaming was requested). Unlike those two call sites, no
    tool schema is forced here — this is a single free-text summarization
    for a human to read, not a structured decision the rest of the system
    acts on.
    """
    try:
        client = build_router_client(settings.router_api_key, settings.router_base_url)
    except RouterNotConfiguredError as exc:
        raise UpgradeProcedureSummaryError(str(exc)) from exc

    content = raw_text[:MAX_PROCEDURE_TEXT_CHARS_FOR_AI]
    try:
        async with client.chat.completions.stream(
            model=settings.router_model,
            max_tokens=PROCEDURE_SUMMARY_MAX_TOKENS,
            messages=[
                {"role": "system", "content": _PROCEDURE_SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            timeout=httpx.Timeout(PROCEDURE_SUMMARY_ROUTER_TIMEOUT_SECONDS),
        ) as stream:
            completion = await stream.get_final_completion()
    except AuthenticationError as exc:
        raise UpgradeProcedureSummaryError(
            f"Model {settings.router_model!r} hoặc API key không hợp lệ trên 9router: "
            f"{readable_exception_message(exc)}"
        ) from exc
    except APIConnectionError as exc:
        raise UpgradeProcedureSummaryError(
            f"Không thể kết nối 9router ({settings.router_base_url}). Kiểm tra host/port."
        ) from exc
    except APIError as exc:
        raise UpgradeProcedureSummaryError(
            f"Model {settings.router_model!r} không khả dụng trên 9router: "
            f"{readable_exception_message(exc)}"
        ) from exc
    except Exception as exc:
        raise UpgradeProcedureSummaryError(
            f"Không thể kết nối 9router: {readable_exception_message(exc)}"
        ) from exc

    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise UpgradeProcedureSummaryError(
            f"Phản hồi bị cắt do vượt giới hạn token (max_tokens={PROCEDURE_SUMMARY_MAX_TOKENS})"
        )
    text = (choice.message.content or "").strip()
    if not text:
        raise UpgradeProcedureSummaryError("9router không trả về nội dung tóm tắt")
    return text


def _get_upgrade_procedure_document(session) -> UpgradeProcedureDocument | None:
    return session.get(UpgradeProcedureDocument, 1)


async def _save_upgrade_procedure(filename: str, raw_text: str, user: str) -> None:
    """Upserts the singleton row (id=1) — re-uploading always replaces the
    previous document and its summary/error wholesale, same posture as
    WatcherHeartbeat's singleton row elsewhere in this codebase."""
    try:
        summary_text = await _summarize_upgrade_procedure(raw_text)
        summary_error = None
    except UpgradeProcedureSummaryError as exc:
        summary_text = None
        summary_error = str(exc)

    with db.SessionLocal() as session:
        doc = _get_upgrade_procedure_document(session)
        if doc is None:
            doc = UpgradeProcedureDocument(id=1)
            session.add(doc)
        doc.filename = filename
        doc.raw_text = raw_text
        doc.summary_text = summary_text
        doc.summary_error = summary_error
        doc.uploaded_by = user
        doc.uploaded_at = datetime.utcnow()
        session.commit()


def _safe_command_preview(action_id: str, host: str, params: dict) -> str:
    """Best-effort preview of the FIRST target node's resolved command, for
    display on the pending-approval screen only — the real execution
    (worker/llm/router_client.py::_execute_approved_action) always
    re-resolves the command fresh, per host, at approval time, so this
    string is illustrative, not authoritative (a package-based upgrade's
    command legitimately differs per host — different systemd units get
    discovered/restarted on a mon node vs an osd node).

    Input validity (target_version/package_dir format, exec_mode) is
    already checked by the route BEFORE this is called, so an ExecutorError
    here can only be the per-host SSH unit-discovery call itself failing
    (e.g. this one node unreachable) — that must not block creating the
    proposal (every other node is unaffected), just fall back to a clear
    "preview unavailable" string instead of a real command.
    """
    try:
        command = executor_commands.get_command(action_id, host, params)
        return (
            f"[Xem trước trên node {host} — lệnh thực tế cho từng node được tính lại khi "
            f"thực thi]\n{command}"
        )
    except ExecutorError as exc:
        return (
            f"[Không xem trước được lệnh trên node {host}: {exc} — lệnh thực tế vẫn sẽ được "
            f"tính khi thực thi]"
        )


def is_cluster_upgrade_pending_or_approved(session) -> bool:
    """True while a cluster-upgrade Action (any of the 3 action_ids — see
    CLUSTER_UPGRADE_ACTION_IDS) has been proposed but not yet resolved
    either way — PENDING_APPROVAL (awaiting the admin) or APPROVED
    (awaiting the Worker's next poll tick to actually start executing).
    This is the window where approving some OTHER risky action (e.g.
    restart_osd_daemon) could race with an upgrade that's about to begin —
    used both to disable "Duyệt" for other pending actions on the main
    Dashboard (index.html) and as the authoritative server-side gate in
    dashboard/routes/actions.py::approve_action.
    """
    return (
        session.query(Action)
        .filter(Action.action_id.in_(CLUSTER_UPGRADE_ACTION_IDS))
        .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
        .first()
        is not None
    )


def is_cluster_upgrade_physically_running() -> bool:
    """Best-effort live check for the window AFTER the Worker has already
    told cephadm to start (Action.status is already EXECUTED by then —
    "sent the start command successfully", not "the upgrade finished" — see
    this module's docstring notes — so is_cluster_upgrade_pending_or_approved()
    alone can't see this phase at all).

    Only meaningful for cephadm (`ceph orch upgrade status`) — a package-
    based (ceph-deploy) upgrade has no orchestrator/status API to query at
    all, so this always returns False for that deployment style; the
    DB-based check above is the only signal available for it.

    Fails OPEN (returns False) on any error — this is a secondary
    convenience gate, not the primary safety guarantee (the kill-switch,
    checked fresh before every remediation command, still is that). A
    transient connectivity hiccup here must not block approving every OTHER
    risky action system-wide just because the Upgrade page happened to be
    unreachable for a moment.
    """
    if settings.ceph_exec_mode != "cephadm":
        return False
    try:
        return bool(get_upgrade_status().get("in_progress"))
    except CephQueryError:
        return False


def _latest_upgrade_action(session) -> tuple[Action | None, Incident | None]:
    action = (
        session.query(Action)
        .filter(Action.action_id.in_(CLUSTER_UPGRADE_ACTION_IDS))
        .order_by(Action.created_at.desc())
        .first()
    )
    if action is None:
        return None, None
    return action, session.get(Incident, action.incident_id)


def _format_step_timestamp(value: str | None) -> str:
    """Progress entries store timestamps as plain UTC ISO strings (JSON
    can't hold a datetime) — parse back to a real datetime just long enough
    to reuse the same dd/mm/YYYY HH:MM:SS Vietnam-local rendering every
    other *_at column on this Dashboard already uses (dashboard/vntime.py).
    """
    if not value:
        return "—"
    try:
        return format_vn(datetime.fromisoformat(value))
    except ValueError:
        return value


def _format_gib(bytes_value: object) -> str:
    """Bytes -> "N GiB" (1 decimal place) for the post-upgrade summary's
    capacity line — pgmap's bytes_total/bytes_used are raw byte counts, not
    human-readable on their own."""
    if not isinstance(bytes_value, (int, float)):
        return "—"
    return f"{bytes_value / (1024 ** 3):.1f} GiB"


def _format_duration(start: datetime, end: datetime) -> str:
    """created_at -> updated_at as "X phút Y giây" — updated_at is the last
    write to this Action row, which for a resolved (EXECUTED/FAILED) action
    is the exact moment _record_approved_execution_result finished, so this
    is the real wall-clock duration of the whole attempt, not just one
    phase."""
    total_seconds = max(0, int((end - start).total_seconds()))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes} phút {seconds} giây"


def _build_post_upgrade_summary_lines() -> list[str]:
    """Live `ceph -s` snapshot (verified against a real cephadm/quincy
    cluster) — best-effort: the upgrade itself already succeeded by the
    time this is called (only invoked for an EXECUTED action), so a
    transient query failure here must not make the whole log look like a
    failure, just skip the summary with a clear note."""
    try:
        _host, status = run_ceph_json_command("ceph -s")
    except CephQueryError as exc:
        return [f"Không lấy được trạng thái trực tiếp từ cụm: {exc}", ""]
    if not isinstance(status, dict):
        return ["Không lấy được trạng thái trực tiếp từ cụm (phản hồi không đúng định dạng).", ""]

    health = (status.get("health") or {}).get("status", "—")
    monmap = status.get("monmap") or {}
    mon_count = len(monmap.get("mons") or [])
    mgrmap = status.get("mgrmap") or {}
    active_mgr = mgrmap.get("active_name") or "—"
    standby_mgrs = [s.get("name") for s in (mgrmap.get("standbys") or []) if s.get("name")]
    osdmap = status.get("osdmap") or {}
    pgmap = status.get("pgmap") or {}

    lines = [
        f"- **Sức khoẻ cụm:** {health}",
        f"- **MON:** {mon_count} node",
        f"- **MGR:** active `{active_mgr}`"
        + (f", standby: {', '.join(standby_mgrs)}" if standby_mgrs else " (không có standby)"),
        f"- **OSD:** {osdmap.get('num_osds', '—')} osd, "
        f"{osdmap.get('num_up_osds', '—')} up, {osdmap.get('num_in_osds', '—')} in",
        f"- **Dung lượng:** {_format_gib(pgmap.get('bytes_used'))} / {_format_gib(pgmap.get('bytes_total'))} đã dùng",
        "",
    ]
    try:
        current_version = summarize_cluster_versions().get("current_version")
    except CephQueryError:
        current_version = None
    if current_version:
        lines.insert(0, f"- **Phiên bản đạt được:** Ceph {current_version}")
    return lines


def build_upgrade_log_markdown(action: Action, incident: Incident | None) -> str:
    """Renders the FULL record of one Cluster Upgrade attempt as Markdown —
    proposal text, per-node steps (with start/end time, the exact command
    run, and the real error text on failure), and — for the cephadm method
    only — a live snapshot of `ceph orch upgrade status` at the moment this
    is generated (cephadm has no per-node steps of ITS OWN in this
    codebase: the Worker sends exactly one `ceph orch upgrade start`
    command and the orchestrator inside Ceph's own mgr does the rest,
    outside this process entirely — see this module's `_CEPHADM_PLAN_TEMPLATE`).

    Used both by the /upgrade page's inline "Nhật ký nâng cấp" card and by
    GET /upgrade/log.md's downloadable file — same function, so the two
    never drift apart.
    """
    method_label = _ACTION_ID_LABELS.get(action.action_id, action.action_id)
    status_label = _ACTION_STATUS_LABELS.get(action.status, action.status)

    try:
        action_params = json.loads(action.action_params) if action.action_params else {}
    except (TypeError, ValueError):
        action_params = {}

    try:
        steps = json.loads(action.execution_progress) if action.execution_progress else []
    except (TypeError, ValueError):
        steps = []

    lines = [
        "# Nhật ký nâng cấp cụm Ceph",
        "",
        f"- **Phương thức:** {method_label}",
    ]
    if action_params.get("target_version"):
        lines.append(f"- **Phiên bản đích:** {action_params['target_version']}")
    if action_params.get("package_dir"):
        lines.append(f"- **Thư mục gói:** {action_params['package_dir']}")
    lines.append(f"- **Trạng thái:** {status_label}")
    lines.append(f"- **Đề xuất lúc:** {format_vn(action.created_at)}")
    lines.append(f"- **Cập nhật lần cuối:** {format_vn(action.updated_at)}")
    if incident is not None and incident.log_excerpt:
        lines.append(f"- **Đề xuất bởi:** {incident.log_excerpt}")
    lines.append("")

    if action.status == ActionStatus.EXECUTED.value:
        lines.append("## Tóm tắt cụm sau nâng cấp")
        lines.append("")
        lines.append(f"- **Thời gian nâng cấp:** {_format_duration(action.created_at, action.updated_at)}")
        lines += _build_post_upgrade_summary_lines()

    if action.rationale:
        lines += ["## Quy trình dự kiến", "", "```", action.rationale, "```", ""]

    if steps:
        lines.append("## Các bước thực hiện theo từng node")
        lines.append("")
        # Story 7.2: steps for the 2 package-based action_ids now carry a
        # `phase` key (install/mon/mgr/osd/mds_rgw) — shown inline per step
        # ("giai đoạn"), so a stored Action from BEFORE this story (no
        # `phase` key at all) still renders every field it always had, just
        # with the same fallback label _phase_label() gives the live
        # progress list above. cephadm's own log (action_id ==
        # "upgrade_ceph_cluster") stays byte-for-byte unchanged — its steps
        # never went through the phased executor, so no phase tag at all.
        show_phase_tag = action.action_id != CLUSTER_UPGRADE_ACTION_ID
        for i, step in enumerate(steps, start=1):
            step_status = step.get("status", "pending")
            status_text = _STEP_STATUS_LABELS.get(step_status, step_status)
            phase_suffix = f" ({_phase_label(step)})" if show_phase_tag else ""
            lines.append(f"{i}. **{step.get('host', '?')}**{phase_suffix} — {status_text}")
            started = step.get("started_at")
            finished = step.get("finished_at")
            if started or finished:
                lines.append(
                    f"   - Thời gian: {_format_step_timestamp(started)} → {_format_step_timestamp(finished)}"
                )
            if step.get("command"):
                lines.append(f"   - Lệnh: `{step['command']}`")
            if step.get("error"):
                lines.append(f"   - Lỗi: `{step['error']}`")
        lines.append("")

    if action.action_id == CLUSTER_UPGRADE_ACTION_ID and action.status == ActionStatus.EXECUTED.value:
        lines.append("## Trạng thái cephadm tại thời điểm tải nhật ký này")
        lines.append("")
        try:
            live_status = get_upgrade_status()
        except CephQueryError as exc:
            lines.append(f"Không lấy được trạng thái trực tiếp từ cụm: {exc}")
        else:
            if live_status.get("in_progress"):
                lines.append(f"- Đang nâng cấp lên: `{live_status.get('target_image') or '—'}`")
                lines.append(f"- Tiến độ: {live_status.get('progress') or '—'}")
                if live_status.get("message"):
                    lines.append(f"- Thông báo: {live_status['message']}")
                if live_status.get("services_complete"):
                    lines.append(f"- Đã hoàn tất: {', '.join(live_status['services_complete'])}")
            else:
                lines.append("Không có tiến trình nâng cấp nào đang chạy trên cụm hiện tại.")
        lines.append("")

    return "\n".join(lines)


@router.get("/upgrade/log.md")
async def download_upgrade_log(user: str = Depends(require_login)):
    """Downloadable .md counterpart to the inline "Nhật ký nâng cấp" card on
    /upgrade — same build_upgrade_log_markdown() output, just served as a
    file instead of rendered in the page."""
    with db.SessionLocal() as session:
        action, incident = _latest_upgrade_action(session)
        if action is None:
            raise HTTPException(status_code=404, detail="Chưa có lần nâng cấp cụm nào được đề xuất.")
        markdown = build_upgrade_log_markdown(action, incident)

    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="nhat-ky-nang-cap-cum.md"'},
    )


def _reject_duplicate_proposal(session) -> None:
    existing, _ = _latest_upgrade_action(session)
    if existing is not None and existing.status in _IN_FLIGHT_ACTION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Đã có một đề xuất nâng cấp đang chờ duyệt hoặc đã duyệt — không thể tạo thêm.",
        )


@router.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request, user: str = Depends(require_login), tab: str = "upgrade"):
    try:
        with db.SessionLocal() as session:
            last_action, last_incident = _latest_upgrade_action(session)
            procedure_document = _get_upgrade_procedure_document(session)
    except SQLAlchemyError:
        logger.exception("upgrade_page: failed to query DB")
        raise HTTPException(
            status_code=503,
            detail="Không kết nối được database — đã chạy `alembic upgrade head` chưa?",
        )

    exec_mode = settings.ceph_exec_mode
    supports_cephadm = exec_mode == "cephadm"
    supports_package = exec_mode == "none"
    upgrade_unsupported = not supports_cephadm and not supports_package

    pending_action = (
        last_action if last_action is not None and last_action.status in _IN_FLIGHT_ACTION_STATUSES else None
    )

    last_action_params: dict = {}
    if last_action is not None and last_action.action_params:
        try:
            last_action_params = json.loads(last_action.action_params) or {}
        except (TypeError, ValueError):
            last_action_params = {}

    # 2026-07-24: package-based upgrade (exec_mode=none) has no orchestrator
    # to poll for progress the way cephadm's `upgrade_progress` below does —
    # this is worker/llm/router_client.py::_execute_approved_action's own
    # per-host progress trail (Action.execution_progress), the only signal
    # available while a real `dnf/apt install` is running (can take minutes
    # per host with nothing else visible in between). Read off `last_action`
    # rather than `pending_action` (2026-07-27) so this same list keeps
    # rendering after the Action resolves to EXECUTED/FAILED too, not just
    # while still PENDING_APPROVAL/APPROVED — pending_action is the exact
    # same row while in-flight, so this is a no-op for that case.
    package_upgrade_progress = None
    if last_action is not None and last_action.execution_progress:
        try:
            package_upgrade_progress = json.loads(last_action.execution_progress)
        except (TypeError, ValueError):
            package_upgrade_progress = None
        else:
            # 2026-07-28: the live list on this page (upgrade.html's
            # "Tiến trình theo từng node") only ever rendered host/status/
            # error — started_at/finished_at have been recorded here since
            # 2026-07-27 (see worker/llm/router_client.py) but only ever
            # surfaced in the post-completion Markdown log below, never
            # while the upgrade is actually running. Reusing
            # _format_step_timestamp (same Vietnam-local rendering as the
            # Markdown log) so an operator watching a multi-minute package
            # install has some indication it's progressing, not just stuck
            # on "Đang chạy" with no clock reference at all.
            for item in package_upgrade_progress:
                item["started_at_display"] = _format_step_timestamp(item.get("started_at"))
                item["finished_at_display"] = _format_step_timestamp(item.get("finished_at"))
                # Story 7.2: phase-grouped display — falls back to "Cài đặt"
                # for both a genuinely-old (pre-7.2) stored Action AND this
                # story's own flags/finalize steps (deliberately never
                # phase-tagged — see _set_upgrade_osd_flags' own comment),
                # so neither crashes nor renders a blank phase column.
                item["phase_label"] = _phase_label(item)

    # 2026-07-27: full Markdown step log ("ghi lại từng bước, từng lỗi
    # trong quá trình cài đặt") — only once the Action has actually
    # resolved (EXECUTED/FAILED), same build_upgrade_log_markdown() the
    # downloadable GET /upgrade/log.md route uses, so the inline preview
    # and the downloaded file are always identical.
    upgrade_log_markdown = None
    if last_action is not None and last_action.status in (
        ActionStatus.EXECUTED.value,
        ActionStatus.FAILED.value,
    ):
        upgrade_log_markdown = build_upgrade_log_markdown(last_action, last_incident)

    current_versions = None
    versions_error = None
    suggested_target = None
    if (supports_cephadm or supports_package) and pending_action is None:
        try:
            current_versions = summarize_cluster_versions()
            if current_versions.get("current_version"):
                suggested_target = propose_next_version(current_versions["current_version"])
        except CephQueryError as exc:
            versions_error = str(exc)

    upgrade_progress = None
    progress_error = None
    if supports_cephadm:
        try:
            upgrade_progress = get_upgrade_status()
        except CephQueryError as exc:
            progress_error = str(exc)

    package_nodes = [n["host"] for n in configured_nodes()] if supports_package else []

    return templates.TemplateResponse(
        request,
        "upgrade.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "active_tab": tab if tab in ("docs", "upgrade") else "upgrade",
            "ceph_exec_mode": exec_mode,
            "supports_cephadm": supports_cephadm,
            "supports_package": supports_package,
            "upgrade_unsupported": upgrade_unsupported,
            "pending_action": pending_action,
            "pending_incident": last_incident if pending_action is not None else None,
            "current_versions": current_versions,
            "versions_error": versions_error,
            "suggested_target": suggested_target,
            "upgrade_progress": upgrade_progress,
            "progress_error": progress_error,
            "package_upgrade_progress": package_upgrade_progress,
            "last_action": last_action,
            "last_action_target_version": last_action_params.get("target_version"),
            "last_action_package_dir": last_action_params.get("package_dir"),
            # Story 7.2: only ever True for the two package-based action_ids
            # (the cephadm propose form has no checkbox at all — see
            # RUN_TEST_SUITE_PARAM_KEY's own comment) — reused for both the
            # still-pending screen (pending_action IS last_action while
            # in-flight) and the resolved-result screen.
            "run_test_suite_requested": bool(last_action_params.get(RUN_TEST_SUITE_PARAM_KEY)),
            "test_runner_frontend_url": settings.test_runner_frontend_url or TEST_RUNNER_FRONTEND_URL,
            "upgrade_log_markdown": upgrade_log_markdown,
            "package_nodes": package_nodes,
            "procedure_document": procedure_document,
            "codenames": codenames_oldest_first(),
            "versions_by_codename": versions_by_codename(),
        },
    )


@router.post("/upgrade/propose")
async def propose_upgrade(target_version: str = Form(...), user: str = Depends(require_login)):
    """cephadm path — creates a PENDING_APPROVAL Action the exact same way
    dashboard/routes/chat.py::confirm_chat_action does for a chat proposal —
    a synthetic Incident, an Action row, one audit.record() call. From here
    the row is indistinguishable from any other RISKY action: it shows up
    in index.html's "Chờ duyệt" card and /upgrade's own pending-approval
    view, and POST /actions/{id}/approve|reject (unchanged) is what the
    admin actually clicks to approve or reject it.
    """
    target_version = target_version.strip()
    if not _TARGET_VERSION_RE.match(target_version):
        raise HTTPException(status_code=400, detail="Phiên bản không hợp lệ (định dạng x.y.z, vd 19.2.0)")
    if settings.ceph_exec_mode != "cephadm":
        raise HTTPException(
            status_code=400,
            detail="Chỉ hỗ trợ nâng cấp qua cephadm cho cụm dùng ceph_exec_mode=cephadm",
        )

    with db.SessionLocal() as session:
        _reject_duplicate_proposal(session)

        mon_nodes = [h.strip() for h in settings.ceph_mon_nodes.split(",") if h.strip()]
        if not mon_nodes:
            raise HTTPException(status_code=400, detail="Chưa cấu hình MON node nào (xem trang Cài đặt)")
        target_node = mon_nodes[0]
        action_params = {"target_version": target_version}

        try:
            resolved_command = executor_commands.get_command(
                CLUSTER_UPGRADE_ACTION_ID, target_node, action_params
            )
        except ExecutorError as exc:
            raise HTTPException(status_code=400, detail=f"Không tạo được lệnh nâng cấp: {exc}")

        classification = gate.classify_action(CLUSTER_UPGRADE_ACTION_ID)  # always RISKY (AD-5)

        incident = Incident(
            ceph_code=CLUSTER_UPGRADE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Đề xuất nâng cấp cụm (cephadm) lên {target_version} bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=CLUSTER_UPGRADE_ACTION_ID,
            classification=classification.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_upgrade_plan_text(target_version),
            target_nodes=json.dumps([target_node]),
            action_params=json.dumps(action_params),
            proposed_command=resolved_command,
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

    return RedirectResponse(url="/upgrade", status_code=303)


@router.post("/upgrade/propose-package-download")
async def propose_package_download_upgrade(
    target_version: str = Form(...),
    run_test_suite: bool = Form(False),
    user: str = Depends(require_login),
):
    """ceph-deploy path, option 1 — download the target release from
    download.ceph.com on each configured node (mon/mgr/osd/rgw, in that
    priority order — shared/cluster_nodes.py::configured_nodes) and restart
    whatever daemons that node runs. See worker/executor/commands.py's
    `_upgrade_ceph_cluster_package_download_command` for the actual
    per-host command, and this module's `_PACKAGE_METHOD_SAFETY_NOTE` for
    why this has no cephadm-style orchestrator gating between nodes.

    Story 7.2: `run_test_suite` is UI/flag-only (see RUN_TEST_SUITE_PARAM_KEY's
    own comment) — stored on the Action only when checked (an unchecked box
    omits the key entirely, keeping action_params identical to before this
    checkbox existed), never wired to an actual test-execution call here.
    """
    target_version = target_version.strip()
    if not _TARGET_VERSION_RE.match(target_version):
        raise HTTPException(status_code=400, detail="Phiên bản không hợp lệ (định dạng x.y.z, vd 19.2.0)")
    if settings.ceph_exec_mode != "none":
        raise HTTPException(
            status_code=400,
            detail="Chỉ áp dụng cho cụm kiểu ceph-deploy (ceph_exec_mode=none)",
        )
    codename = codename_for_version(target_version)
    if codename is None:
        raise HTTPException(
            status_code=400,
            detail=f"Không tìm thấy mã tên release Ceph cho phiên bản {target_version!r}",
        )

    with db.SessionLocal() as session:
        _reject_duplicate_proposal(session)

        target_nodes = [n["host"] for n in configured_nodes()]
        if not target_nodes:
            raise HTTPException(status_code=400, detail="Chưa cấu hình node nào (xem trang Cài đặt)")

        action_params = {"target_version": target_version}
        preview_command = await asyncio.to_thread(
            _safe_command_preview, PACKAGE_DOWNLOAD_ACTION_ID, target_nodes[0], action_params
        )
        if run_test_suite:
            action_params[RUN_TEST_SUITE_PARAM_KEY] = True

        incident = Incident(
            ceph_code=CLUSTER_UPGRADE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=(
                f"Đề xuất nâng cấp cụm (ceph-deploy, tải từ download.ceph.com) lên "
                f"{target_version} bởi {user}"
            ),
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()

        action = Action(
            incident_id=incident.id,
            action_id=PACKAGE_DOWNLOAD_ACTION_ID,
            classification=gate.classify_action(PACKAGE_DOWNLOAD_ACTION_ID).value,  # always RISKY
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_package_download_plan_text(target_version, codename, target_nodes),
            target_nodes=json.dumps(target_nodes),
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

    return RedirectResponse(url="/upgrade", status_code=303)


@router.post("/upgrade/propose-package-local")
async def propose_package_local_upgrade(
    package_dir: str = Form(...),
    run_test_suite: bool = Form(False),
    user: str = Depends(require_login),
):
    """ceph-deploy path, option 2 — install from a directory of packages
    already staged on each node (no download, no scp — the operator is
    responsible for having put the right packages at the SAME path on
    every configured node beforehand). See propose_package_download_upgrade's
    own docstring for `run_test_suite`'s Story 7.2 semantics."""
    package_dir = package_dir.strip()
    if not _PACKAGE_DIR_RE.match(package_dir):
        raise HTTPException(
            status_code=400,
            detail="Đường dẫn không hợp lệ (phải là đường dẫn tuyệt đối, vd /opt/ceph-packages)",
        )
    if settings.ceph_exec_mode != "none":
        raise HTTPException(
            status_code=400,
            detail="Chỉ áp dụng cho cụm kiểu ceph-deploy (ceph_exec_mode=none)",
        )

    with db.SessionLocal() as session:
        _reject_duplicate_proposal(session)

        target_nodes = [n["host"] for n in configured_nodes()]
        if not target_nodes:
            raise HTTPException(status_code=400, detail="Chưa cấu hình node nào (xem trang Cài đặt)")

        action_params = {"package_dir": package_dir}
        preview_command = await asyncio.to_thread(
            _safe_command_preview, PACKAGE_LOCAL_ACTION_ID, target_nodes[0], action_params
        )
        if run_test_suite:
            action_params[RUN_TEST_SUITE_PARAM_KEY] = True

        incident = Incident(
            ceph_code=CLUSTER_UPGRADE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=(
                f"Đề xuất nâng cấp cụm (ceph-deploy, gói cục bộ tại {package_dir}) bởi {user}"
            ),
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()

        action = Action(
            incident_id=incident.id,
            action_id=PACKAGE_LOCAL_ACTION_ID,
            classification=gate.classify_action(PACKAGE_LOCAL_ACTION_ID).value,  # always RISKY
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_package_local_plan_text(package_dir, target_nodes),
            target_nodes=json.dumps(target_nodes),
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

    return RedirectResponse(url="/upgrade", status_code=303)


@router.post("/upgrade/procedure/upload")
async def upload_upgrade_procedure(file: UploadFile = File(...), user: str = Depends(require_login)):
    """Operator uploads their own upgrade runbook (plain text/markdown only —
    no PDF/Word parser in this codebase). Saved regardless of whether 9router
    is configured/reachable; the AI summary is best-effort (see
    _save_upgrade_procedure) and shown alongside the system's own generated
    plan text on the pending-approval screen (upgrade.html) once proposed.
    """
    filename = file.filename or "upload.txt"
    if not filename.lower().endswith(ALLOWED_PROCEDURE_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Chỉ hỗ trợ file văn bản thuần ({', '.join(ALLOWED_PROCEDURE_EXTENSIONS)}) — "
                f"PDF/Word chưa được hỗ trợ."
            ),
        )

    raw_bytes = await file.read()
    if len(raw_bytes) > MAX_PROCEDURE_FILE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File quá lớn (tối đa {MAX_PROCEDURE_FILE_BYTES // (1024 * 1024)}MB).",
        )
    try:
        raw_text = raw_bytes.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Không đọc được nội dung file — cần file văn bản UTF-8 thuần (.txt/.md).",
        )
    if not raw_text:
        raise HTTPException(status_code=400, detail="File rỗng.")

    await _save_upgrade_procedure(filename, raw_text, user)
    return RedirectResponse(url="/upgrade?tab=docs", status_code=303)


@router.post("/upgrade/procedure/resummarize")
async def resummarize_upgrade_procedure(user: str = Depends(require_login)):
    """Retries AI summarization on the already-uploaded document's stored
    raw_text — no re-upload needed, e.g. after fixing 9router config
    following an upload that saved with a summary_error."""
    with db.SessionLocal() as session:
        doc = _get_upgrade_procedure_document(session)
        if doc is None:
            raise HTTPException(
                status_code=404, detail="Chưa có tài liệu quy trình nâng cấp nào được upload."
            )
        filename, raw_text = doc.filename, doc.raw_text

    await _save_upgrade_procedure(filename, raw_text, user)
    return RedirectResponse(url="/upgrade?tab=docs", status_code=303)


@router.post("/upgrade/pause")
async def pause_upgrade_route(user: str = Depends(require_login)):
    try:
        pause_upgrade()
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không tạm dừng được: {exc}")
    with db.SessionLocal() as session:
        _, incident = _latest_upgrade_action(session)
        if incident is not None:
            audit.record(
                session,
                incident_id=incident.id,
                action_id=None,
                event_type=audit.EVENT_CLUSTER_UPGRADE_PAUSED,
                actor=user,
            )
            session.commit()
    return RedirectResponse(url="/upgrade", status_code=303)


@router.post("/upgrade/resume")
async def resume_upgrade_route(user: str = Depends(require_login)):
    try:
        resume_upgrade()
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không tiếp tục được: {exc}")
    with db.SessionLocal() as session:
        _, incident = _latest_upgrade_action(session)
        if incident is not None:
            audit.record(
                session,
                incident_id=incident.id,
                action_id=None,
                event_type=audit.EVENT_CLUSTER_UPGRADE_RESUMED,
                actor=user,
            )
            session.commit()
    return RedirectResponse(url="/upgrade", status_code=303)


@router.post("/upgrade/unset-osd-flags")
async def unset_upgrade_osd_flags_route(user: str = Depends(require_login)):
    """Manual cleanup for the cephadm path — worker/executor/commands.py::
    _upgrade_ceph_cluster_command sets noout/noscrub/nodeep-scrub/
    nosnaptrim before starting `ceph orch upgrade start`, but can't unset
    them itself (fire-and-forget; the real upgrade continues asynchronously
    in cephadm's own mgr module long after that command returns — see that
    function's own docstring). Same self-service, not-kill-switch-gated
    posture as pause/resume just above: an operator explicitly clicking
    this once they see (via this page's own live status, `get_upgrade_
    status()`) that the upgrade has actually finished."""
    try:
        unset_upgrade_osd_flags()
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không bỏ được các cờ noout/noscrub: {exc}")
    with db.SessionLocal() as session:
        _, incident = _latest_upgrade_action(session)
        if incident is not None:
            audit.record(
                session,
                incident_id=incident.id,
                action_id=None,
                event_type=audit.EVENT_CLUSTER_UPGRADE_OSD_FLAGS_UNSET,
                actor=user,
            )
            session.commit()
    return RedirectResponse(url="/upgrade", status_code=303)
