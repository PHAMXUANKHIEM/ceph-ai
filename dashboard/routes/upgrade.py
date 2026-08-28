import asyncio
import json
import logging
import re
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from openai import APIError, APIConnectionError, AuthenticationError
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from dashboard.vntime import format_vn
from shared import audit, db
from shared.ceph_releases import (
    EL_FAMILY_OS_IDS,
    codename_for_version,
    codenames_oldest_first,
    min_os_label_for,
    os_upgrade_warning,
    versions_by_codename,
)
from shared.cluster_nodes import configured_nodes
from shared.models import (
    Action,
    ActionStatus,
    Incident,
    IncidentStatus,
    NodeUpgradeGate,
    NodeUpgradeGateState,
    UpgradeProcedureDocument,
)
from shared.node_upgrade_gate import claim_node_upgrade_gate_lock
from shared.router_client import RouterNotConfiguredError, build_router_client, readable_exception_message
from shared.ai_redaction import redact_text
from shared.ai_observability import observe_ai_call, record_ai_usage
from watcher.ceph_client import (
    CephQueryError,
    get_upgrade_status,
    get_upgrade_status_with,
    pause_upgrade,
    pause_upgrade_with,
    propose_next_version,
    resume_upgrade,
    resume_upgrade_with,
    run_ceph_json_command,
    run_ceph_json_command_with,
    summarize_cluster_versions,
    summarize_versions_payload,
    unset_upgrade_osd_flags,
    unset_upgrade_osd_flags_with,
)
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError, read_os_release
from worker.policy import gate

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


# Imported lazily because dashboard.routes.incidents imports this module's
# upgrade gate helpers; importing cluster_scope here would form a cycle.
def _cluster_selection(request: Request):
    from dashboard.cluster_scope import cluster_selection

    return cluster_selection(request)


def _selected_cluster(request: Request):
    from dashboard.cluster_scope import selected_cluster

    return selected_cluster(request)


def _cluster_connection(cluster):
    from dashboard.cluster_scope import cluster_connection

    return cluster_connection(cluster)


def _effective_exec_mode(cluster) -> str:
    return settings.ceph_exec_mode if cluster.is_default else cluster.ceph_exec_mode


def _effective_nodes(cluster) -> list[dict]:
    return configured_nodes() if cluster.is_default else configured_nodes(cluster)


def _effective_mon_nodes(cluster) -> list[str]:
    raw = settings.ceph_mon_nodes if cluster.is_default else cluster.ceph_mon_nodes
    return [host.strip() for host in raw.split(",") if host.strip()]


def _upgrade_url(cluster) -> str:
    return "/upgrade" if cluster.is_default else f"/upgrade?cluster={cluster.id}"

# Synthetic Incident.ceph_code for this feature — same trick
# dashboard/routes/chat.py uses (CHAT_REQUEST_CEPH_CODE): AuditEntry.incident_id
# is a required FK (AD-7), and this feature has no real detected Incident
# behind it, only an operator explicitly proposing an upgrade. Reusing the
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

# Epic 11 (OS Upgrade Gate + Node OS Reinstall/Ceph Recovery), Story 11.3 —
# same synthetic-Incident trick as CLUSTER_UPGRADE_CEPH_CODE above.
NODE_OS_GATE_CEPH_CODE = "NODE_OS_GATE"
NODE_OS_GATE_PREPARE_ACTION_ID = "node_os_gate_prepare"
NODE_OS_GATE_ABORT_ACTION_ID = "node_os_gate_abort"
NODE_OS_GATE_RECOVER_ACTION_ID = "node_os_gate_recover"
# NodeUpgradeGate.state values that block a second Prepare for the SAME
# host (FR-7's idempotency: a re-click while already PREPARING/PREPARED
# must not re-run FR-3/4/5) — DONE/FAILED are terminal and allow a fresh
# attempt.
_NODE_OS_GATE_NON_TERMINAL_STATES = (
    NodeUpgradeGateState.PREPARING.value,
    NodeUpgradeGateState.PREPARED.value,
    NodeUpgradeGateState.RECOVERING.value,
    NodeUpgradeGateState.ABORTING.value,
)

_TARGET_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PACKAGE_DIR_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)

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

khi cephadm đã bắt đầu điều phối nâng cấp, việc dừng lại giữa chừng cần bấm "Tạm dừng" ở trên."""

# 2026-07-23: shared tail for both package-based (ceph-deploy) plan texts
# below — the execution-model caveat that does NOT apply to the cephadm
# plan above (there IS an orchestrator gating cephadm's steps on cluster
# health; there is none here — see worker/executor/commands.py's module
# comment on the package-based builders for the full reasoning).
_PACKAGE_METHOD_SAFETY_NOTE = """\
QUAN TRỌNG — khác với cephadm: KHÔNG có orchestrator tự kiểm tra sức khoẻ cụm giữa các bước.
Nếu một bước gặp lỗi, hệ thống VẪN thử tiếp bước kế tiếp (không tự dừng lại). Hãy theo dõi sát
Audit Trail trong suốt quá trình, đặc biệt với cụm nhiều node."""


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


@observe_ai_call("upgrade_summary", backend="router")
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

    content = redact_text(raw_text[:MAX_PROCEDURE_TEXT_CHARS_FOR_AI])
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
        record_ai_usage(completion)
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


def _check_os_upgrade_needed(target_version: str, target_nodes: list[str]) -> list[dict]:
    """Best-effort OS-vs-target-Ceph-version preflight for the package-
    based (ceph-deploy/download.ceph.com) upgrade path — the CentOS 7 ->
    Ceph Pacific case: that install would otherwise only fail live, mid-run,
    when worker/executor/commands.py's repo-URL builder 404s on the target
    host (see shared/ceph_releases.py's min_el_version/os_upgrade_warning
    for the underlying table/logic). Returns one dict
    (`{"host", "os_id", "os_version_id", "warning"}`) per incompatible node
    — empty = every checked node is OK to proceed. Structured (Story 11.1,
    was a plain list[str] before) so the Gate screen (AD-20) can render
    current-OS/minimum-OS as their own table columns instead of re-parsing
    `warning`'s prose.

    Deliberately scoped to THIS upgrade flavor only — cephadm
    (propose_upgrade) pulls versioned container images, not an el-specific
    RPM/APT repo, so this OS-floor concern doesn't apply there; the local-
    package-dir flavor (propose_package_local_upgrade) never has a known
    target_version to check against in the first place (the operator
    supplies an arbitrary directory, not a version string).

    A node that's unreachable/unparseable is SKIPPED, not reported as
    incompatible — same best-effort posture as _safe_command_preview right
    below: an unrelated SSH hiccup checking THIS must never be the reason a
    real, valid upgrade proposal gets blocked. That node's real command
    still fails loud on its own at execution time if it truly can't be
    reached, same as today.
    """
    incompatible: list[dict] = []
    for host in target_nodes:
        try:
            os_release = read_os_release(host)
        except ExecutorError:
            continue
        os_id = (os_release.get("ID") or "").lower()
        os_version_id = os_release.get("VERSION_ID") or ""
        warning = os_upgrade_warning(target_version, os_id, os_version_id)
        if warning:
            incompatible.append(
                {"host": host, "os_id": os_id, "os_version_id": os_version_id, "warning": warning}
            )
    return incompatible


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


def _latest_upgrade_action(session, cluster=None) -> tuple[Action | None, Incident | None]:
    query = (
        session.query(Action)
        .join(Incident, Incident.id == Action.incident_id)
        .filter(Action.action_id.in_(CLUSTER_UPGRADE_ACTION_IDS))
    )
    if cluster is not None:
        if cluster.is_default:
            query = query.filter(or_(Incident.cluster_id == cluster.id, Incident.cluster_id.is_(None)))
        else:
            query = query.filter(Incident.cluster_id == cluster.id)
    action = query.order_by(Action.created_at.desc()).first()
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
async def download_upgrade_log(request: Request, user: str = Depends(require_login)):
    """Downloadable .md counterpart to the inline "Nhật ký nâng cấp" card on
    /upgrade — same build_upgrade_log_markdown() output, just served as a
    file instead of rendered in the page."""
    with db.SessionLocal() as session:
        cluster = _selected_cluster(request)
        action, incident = _latest_upgrade_action(session, cluster)
        if action is None:
            raise HTTPException(status_code=404, detail="Chưa có lần nâng cấp cụm nào được đề xuất.")
        markdown = build_upgrade_log_markdown(action, incident)

    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="nhat-ky-nang-cap-cum.md"'},
    )


def _reject_duplicate_proposal(session, cluster=None) -> None:
    existing, _ = _latest_upgrade_action(session, cluster)
    if existing is not None and existing.status in _IN_FLIGHT_ACTION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Đã có một đề xuất nâng cấp đang chờ duyệt hoặc đã duyệt — không thể tạo thêm.",
        )


@router.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request, user: str = Depends(require_login), tab: str = "upgrade"):
    try:
        clusters, cluster = _cluster_selection(request)
        with db.SessionLocal() as session:
            last_action, last_incident = _latest_upgrade_action(session, cluster)
            procedure_document = _get_upgrade_procedure_document(session)
    except SQLAlchemyError:
        logger.exception("upgrade_page: failed to query DB")
        raise HTTPException(
            status_code=503,
            detail="Không kết nối được database — đã chạy `alembic upgrade head` chưa?",
        )

    exec_mode = _effective_exec_mode(cluster)
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
            if cluster.is_default:
                current_versions = summarize_cluster_versions()
            else:
                _, versions_payload = run_ceph_json_command_with(
                    *_cluster_connection(cluster), "ceph versions"
                )
                current_versions = summarize_versions_payload(versions_payload)
            if current_versions.get("current_version"):
                suggested_target = propose_next_version(current_versions["current_version"])
        except CephQueryError as exc:
            versions_error = str(exc)

    upgrade_progress = None
    progress_error = None
    if supports_cephadm:
        try:
            upgrade_progress = (
                get_upgrade_status() if cluster.is_default else get_upgrade_status_with(*_cluster_connection(cluster))
            )
        except CephQueryError as exc:
            progress_error = str(exc)

    package_nodes = [n["host"] for n in _effective_nodes(cluster)] if supports_package else []

    return templates.TemplateResponse(
        request,
        "upgrade.html",
        {
            "user": user,
            "clusters": clusters,
            "selected_cluster": cluster,
            "upgrade_cluster_query": "" if cluster.is_default else f"?cluster={cluster.id}",
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
            "upgrade_log_markdown": upgrade_log_markdown,
            "package_nodes": package_nodes,
            "procedure_document": procedure_document,
            "codenames": codenames_oldest_first(),
            "versions_by_codename": versions_by_codename(),
        },
    )


@router.post("/upgrade/propose")
async def propose_upgrade(request: Request, target_version: str = Form(...), user: str = Depends(require_login)):
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
    cluster = _selected_cluster(request)
    if _effective_exec_mode(cluster) != "cephadm":
        raise HTTPException(
            status_code=400,
            detail="Chỉ hỗ trợ nâng cấp qua cephadm cho cụm dùng ceph_exec_mode=cephadm",
        )

    with db.SessionLocal() as session:
        _reject_duplicate_proposal(session, cluster)

        mon_nodes = _effective_mon_nodes(cluster)
        if not mon_nodes:
            raise HTTPException(status_code=400, detail="Chưa cấu hình MON node nào (xem trang Cài đặt)")
        target_node = mon_nodes[0]
        action_params = {"target_version": target_version}
        if not cluster.is_default:
            action_params["_cluster_exec_mode"] = cluster.ceph_exec_mode

        try:
            resolved_command = executor_commands.get_command(
                CLUSTER_UPGRADE_ACTION_ID, target_node, action_params
            )
        except ExecutorError as exc:
            raise HTTPException(status_code=400, detail=f"Không tạo được lệnh nâng cấp: {exc}")

        classification = gate.classify_action(CLUSTER_UPGRADE_ACTION_ID)  # always RISKY (AD-5)

        incident = Incident(
            cluster_id=None if cluster.is_default else cluster.id,
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

    return RedirectResponse(url=_upgrade_url(cluster), status_code=303)


async def _build_os_upgrade_gate_context(
    session, user: str, target_version: str, codename: str, incompatible_nodes: list[dict]
) -> dict:
    """Story 11.3: shared context builder for `os_upgrade_gate.html`,
    extracted from `propose_package_download_upgrade`'s own blocked-proposal
    render (Story 11.1) so the new standalone `GET /upgrade/gate` route
    below can render the SAME screen without re-proposing anything.
    `node_gates` maps each node's host to its current (latest, if more than
    one — e.g. a prior FAILED attempt) `NodeUpgradeGate` row, or omits the
    key entirely if the host has never been gated — the template treats a
    missing key the same as `None`.

    Story 11.4 fix (a real gap, not a nice-to-have — see this story's Dev
    Notes): `incompatible_nodes` alone is a FRESH live SSH re-check, so a
    node whose OS just passed (mid-Confirm/Recovery, or even PREPARED right
    after the operator finishes reinstalling but before clicking "Xác
    nhận") would silently vanish from the table. The node LIST rendered
    here is the UNION of `incompatible_nodes` and any host with a
    non-terminal `NodeUpgradeGate` for THIS `target_version` — a node
    mid-flight must stay visible/actionable regardless of what a live OS
    check says right now."""
    incompatible_hosts = {n["host"] for n in incompatible_nodes}
    non_terminal_rows = (
        session.query(NodeUpgradeGate)
        .filter(NodeUpgradeGate.target_version == target_version)
        .filter(NodeUpgradeGate.state.in_(_NODE_OS_GATE_NON_TERMINAL_STATES))
        .all()
    )
    extra_hosts = sorted({row.host for row in non_terminal_rows} - incompatible_hosts)

    nodes = list(incompatible_nodes)
    for host in extra_hosts:
        try:
            # Code-review fix: read_os_release() is a blocking SSH round-
            # trip — this function is called from an async route handler,
            # so it must not block the event loop (same asyncio.to_thread
            # wrapping this route's own _check_os_upgrade_needed call
            # already uses).
            os_release = await asyncio.to_thread(read_os_release, host)
            os_id, os_version_id = os_release.get("ID", "?"), os_release.get("VERSION_ID", "?")
        except ExecutorError:
            os_id, os_version_id = "?", "?"
        nodes.append({"host": host, "os_id": os_id, "os_version_id": os_version_id, "warning": None})

    union_hosts = [n["host"] for n in nodes]
    node_gates: dict[str, NodeUpgradeGate] = {}
    if union_hosts:
        rows = (
            session.query(NodeUpgradeGate)
            .filter(NodeUpgradeGate.host.in_(union_hosts))
            .order_by(NodeUpgradeGate.created_at.desc())
            .all()
        )
        for row in rows:
            node_gates.setdefault(row.host, row)  # newest first (order_by desc) wins per host

    # Story 11.4: a RECOVERING node's Confirm Action carries the live
    # per-phase progress (Action.execution_progress, same shape every other
    # cluster_deploy_action_ids member already writes) — surfaced here as a
    # snapshot-on-page-load table (refresh to see the latest, not a live
    # WS-pushed view — this route/template has no existing JS/WS scaffolding
    # to plug into, and adding one is out of proportion for this story).
    recovering_progress: dict[str, list] = {}
    for host, row in node_gates.items():
        if row.state == NodeUpgradeGateState.RECOVERING.value and row.confirm_action_id:
            action = session.get(Action, row.confirm_action_id)
            if action is not None and action.execution_progress:
                try:
                    recovering_progress[host] = json.loads(action.execution_progress)
                except (TypeError, ValueError):
                    recovering_progress[host] = []

    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "target_version": target_version,
        "codename": codename,
        "min_os_label": min_os_label_for(target_version),
        "incompatible_nodes": nodes,
        "node_gates": node_gates,
        "recovering_progress": recovering_progress,
    }


@router.get("/upgrade/gate", response_class=HTMLResponse)
async def os_upgrade_gate_page(
    request: Request, target_version: str, user: str = Depends(require_login)
):
    """Story 11.3: standalone GET route for the Gate screen — Story 11.1
    only ever rendered `os_upgrade_gate.html` as the response to a BLOCKED
    `POST /upgrade/propose-package-download`, with no way to return to it
    later (e.g. after Preparing node1, to check status or Prepare node2).
    Redirects to `/upgrade` if every node now passes the OS check (nothing
    left to Prepare/Abort here)."""
    target_version = target_version.strip()
    if not _TARGET_VERSION_RE.match(target_version):
        raise HTTPException(status_code=400, detail="Phiên bản không hợp lệ (định dạng x.y.z, vd 19.2.0)")
    codename = codename_for_version(target_version)
    if codename is None:
        raise HTTPException(
            status_code=400,
            detail=f"Không tìm thấy mã tên release Ceph cho phiên bản {target_version!r}",
        )

    target_nodes = [n["host"] for n in configured_nodes()]
    incompatible_nodes = await asyncio.to_thread(_check_os_upgrade_needed, target_version, target_nodes)

    with db.SessionLocal() as session:
        # A node mid-Confirm/Recovery can pass the live OS check before its
        # gate reaches a terminal state (Story 11.4's fix) — only redirect
        # away when there's truly nothing left to Prepare/Confirm/Abort.
        has_non_terminal_gate = (
            session.query(NodeUpgradeGate)
            .filter(NodeUpgradeGate.target_version == target_version)
            .filter(NodeUpgradeGate.state.in_(_NODE_OS_GATE_NON_TERMINAL_STATES))
            .first()
            is not None
        )
        if not incompatible_nodes and not has_non_terminal_gate:
            return RedirectResponse(url="/upgrade", status_code=303)

        context = await _build_os_upgrade_gate_context(
            session, user, target_version, codename.capitalize(), incompatible_nodes
        )
    return templates.TemplateResponse(request, "os_upgrade_gate.html", context)


@router.post("/upgrade/gate/prepare")
async def prepare_node_os_gate(
    host: str = Form(...), target_version: str = Form(...), user: str = Depends(require_login)
):
    """Story 11.3 (AD-21): one click — claims the CAS lock (AD-24), creates
    Incident+Action(node_os_gate_prepare)+NodeUpgradeGate(PREPARING) with
    Dashboard writing all 3 FK columns via prepare_action_id (AD-18), then
    calls `approve_action_core` in the SAME request. `approve_action_core`
    is imported locally (not at module level) to avoid a circular import —
    dashboard/routes/actions.py already imports THIS module as
    `upgrade_routes` for its own two older mutual-exclusion checks."""
    from dashboard.routes.actions import ActionConflictError, approve_action_core

    if settings.ceph_exec_mode != "none":
        raise HTTPException(
            status_code=400, detail="Chỉ áp dụng cho cụm kiểu ceph-deploy (ceph_exec_mode=none)"
        )
    matching = [n for n in configured_nodes() if n["host"] == host]
    if not matching:
        raise HTTPException(status_code=400, detail=f"Node {host!r} không nằm trong danh sách cấu hình")
    roles = matching[0]["roles"]

    with db.SessionLocal() as session:
        existing = (
            session.query(NodeUpgradeGate)
            .filter(NodeUpgradeGate.host == host)
            .filter(NodeUpgradeGate.state.in_(_NODE_OS_GATE_NON_TERMINAL_STATES))
            .first()
        )
        if existing is not None:
            # FR-7: idempotent re-click for a node already PREPARING/PREPARED
            # — do NOT re-run FR-3/4/5, do NOT touch the CAS lock at all.
            return RedirectResponse(url=f"/upgrade/gate?target_version={target_version}", status_code=303)

        gate_id = str(uuid.uuid4())
        if not claim_node_upgrade_gate_lock(session, gate_id):
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Đang có node khác đang trong quá trình Chuẩn bị/chờ Xác nhận/Node Recovery — "
                    "chỉ 1 node được xử lý cùng lúc."
                ),
            )

        incident = Incident(
            ceph_code=NODE_OS_GATE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Chuẩn bị node {host} để cài lại OS (mục tiêu Ceph {target_version}) bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()

        action_params = {"host": host, "target_version": target_version, "roles": roles, "nodes": [host]}
        action = Action(
            incident_id=incident.id,
            action_id=NODE_OS_GATE_PREPARE_ACTION_ID,
            classification=gate.classify_action(NODE_OS_GATE_PREPARE_ACTION_ID).value,  # always RISKY
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"Chuẩn bị node {host} (vai: {', '.join(roles)}) để cài lại OS lên tối thiểu tương "
                f"thích Ceph {target_version}"
            ),
            target_nodes=json.dumps([host]),
        )
        session.add(action)
        session.flush()

        action_params["node_upgrade_gate_id"] = gate_id
        action_params["action_pk"] = action.id
        action_params["incident_id"] = incident.id
        action.action_params = json.dumps(action_params)
        action.proposed_command = await asyncio.to_thread(
            _safe_command_preview, NODE_OS_GATE_PREPARE_ACTION_ID, host, action_params
        )

        session.add(
            NodeUpgradeGate(
                id=gate_id,
                host=host,
                target_version=target_version,
                state=NodeUpgradeGateState.PREPARING.value,
                roles_snapshot=json.dumps(roles),
                prepare_action_id=action.id,
            )
        )

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()
        action_id_for_approval = action.id

    try:
        await asyncio.to_thread(approve_action_core, action_id_for_approval, user)
    except ActionConflictError as exc:
        # Should be provably unreachable (see story Dev Notes: the CAS lock
        # just claimed above already prevents a second gate from existing
        # concurrently, and AD-19's exemption is row-specific to THIS
        # action). Logged loudly since reaching this branch would mean a
        # real bug elsewhere, not a normal runtime condition.
        logger.error(
            "prepare_node_os_gate: approve_action_core unexpectedly raised ActionConflictError for "
            "its own freshly-created gate action %s: %s",
            action_id_for_approval,
            exc.detail,
        )
        raise HTTPException(status_code=409, detail=exc.detail)

    return RedirectResponse(url=f"/upgrade/gate?target_version={target_version}", status_code=303)


@router.post("/upgrade/gate/abort")
async def abort_node_os_gate(host: str = Form(...), user: str = Depends(require_login)):
    """Story 11.3 (FR-6): reverses a PREPARED node back out — only valid
    while the gate is still exactly PREPARED (not available once Confirm
    has been clicked, Story 11.4 — written as an explicit `== PREPARED`
    check, not `!= DONE`, so it fails safe once Recovery states exist
    too)."""
    from dashboard.routes.actions import ActionConflictError, approve_action_core

    with db.SessionLocal() as session:
        gate_row = (
            session.query(NodeUpgradeGate)
            .filter(NodeUpgradeGate.host == host)
            .order_by(NodeUpgradeGate.created_at.desc())
            .first()
        )
        if gate_row is None or gate_row.state != NodeUpgradeGateState.PREPARED.value:
            raise HTTPException(
                status_code=400,
                detail=f"Node {host!r} không ở trạng thái 'Sẵn sàng cài lại OS' — không thể Huỷ Chuẩn bị",
            )

        roles = json.loads(gate_row.roles_snapshot or "[]")
        target_version = gate_row.target_version
        incident = Incident(
            ceph_code=NODE_OS_GATE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Huỷ Chuẩn bị cho node {host} bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()

        action_params = {
            "host": host,
            "target_version": target_version,
            "roles": roles,
            "nodes": [host],
            "node_upgrade_gate_id": gate_row.id,
        }
        action = Action(
            incident_id=incident.id,
            action_id=NODE_OS_GATE_ABORT_ACTION_ID,
            classification=gate.classify_action(NODE_OS_GATE_ABORT_ACTION_ID).value,  # always RISKY
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=f"Huỷ Chuẩn bị cho node {host} (vai: {', '.join(roles)}) — rejoin mon, gỡ cờ nếu là node cuối",
            target_nodes=json.dumps([host]),
        )
        session.add(action)
        session.flush()

        action_params["action_pk"] = action.id
        action_params["incident_id"] = incident.id
        action.action_params = json.dumps(action_params)
        action.proposed_command = await asyncio.to_thread(
            _safe_command_preview, NODE_OS_GATE_ABORT_ACTION_ID, host, action_params
        )

        gate_row.abort_action_id = action.id

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()
        action_id_for_approval = action.id
        gate_target_version = target_version

    try:
        await asyncio.to_thread(approve_action_core, action_id_for_approval, user)
    except ActionConflictError as exc:
        logger.error(
            "abort_node_os_gate: approve_action_core unexpectedly raised ActionConflictError for "
            "its own freshly-created abort action %s: %s",
            action_id_for_approval,
            exc.detail,
        )
        raise HTTPException(status_code=409, detail=exc.detail)

    return RedirectResponse(url=f"/upgrade/gate?target_version={gate_target_version}", status_code=303)


@router.post("/upgrade/gate/confirm")
async def confirm_node_os_gate(host: str = Form(...), user: str = Depends(require_login)):
    """Story 11.4 (FR-8, AD-20, AD-21): re-checks OS live BEFORE creating
    anything (AD-20 — sync, dashboard-side, no Action) — only if that
    passes does it propose+approve `node_os_gate_recover` in the same
    request, exactly like Prepare/Abort. Does NOT claim a new CAS lock —
    the one Prepare claimed is still held (AD-21: "vẫn giữ khoá CAS đã có,
    không release")."""
    from dashboard.routes.actions import ActionConflictError, approve_action_core

    with db.SessionLocal() as session:
        gate_row = (
            session.query(NodeUpgradeGate)
            .filter(NodeUpgradeGate.host == host)
            .order_by(NodeUpgradeGate.created_at.desc())
            .first()
        )
        if gate_row is None:
            raise HTTPException(
                status_code=400, detail=f"Node {host!r} chưa từng được Chuẩn bị — không thể Xác nhận"
            )

        # FR-7-style idempotency: a re-click while Recovery is already
        # mid-flight must not create a second node_os_gate_recover Action —
        # checked BEFORE the OS re-check below (an in-flight Recovery must
        # not be re-triggered just because the operator double-clicked).
        if gate_row.state == NodeUpgradeGateState.RECOVERING.value:
            return RedirectResponse(
                url=f"/upgrade/gate?target_version={gate_row.target_version}", status_code=303
            )

        if gate_row.state != NodeUpgradeGateState.PREPARED.value:
            raise HTTPException(
                status_code=400,
                detail=f"Node {host!r} không ở trạng thái 'Sẵn sàng cài lại OS' — không thể Xác nhận",
            )

        target_version = gate_row.target_version

    # FR-8's re-check, AD-20: sync, no Action, no DB write on failure.
    try:
        os_release = await asyncio.to_thread(read_os_release, host)
    except ExecutorError:
        raise HTTPException(
            status_code=400,
            detail=f"Không kết nối được SSH tới {host!r} để xác nhận lại OS — thử lại sau",
        )

    os_id = (os_release.get("ID") or "").lower()
    os_version_id = os_release.get("VERSION_ID") or ""
    # Code-review fix: os_upgrade_warning() is documented as FAIL-OPEN
    # (returns None — "no warning", NOT "confirmed compatible" — for any
    # os_id outside EL_FAMILY_OS_IDS or an unparseable VERSION_ID). That's
    # the right default for the ORIGINAL best-effort pre-check
    # (_check_os_upgrade_needed), but AC #1 here wants the opposite
    # posture: reject unless genuinely confirmed. Guard explicitly instead
    # of trusting a falsy `warning` alone.
    if os_id not in EL_FAMILY_OS_IDS or not os_version_id.split(".")[0].isdigit():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Không xác định được hệ điều hành trên {host!r} (ID={os_id!r}, "
                f"VERSION_ID={os_version_id!r}) — không thể xác nhận, node giữ nguyên trạng thái cũ"
            ),
        )

    warning = os_upgrade_warning(target_version, os_id, os_version_id)
    if warning:
        # AC #1: node giữ nguyên trạng thái cũ — không tạo gì.
        raise HTTPException(status_code=400, detail=warning)

    with db.SessionLocal() as session:
        gate_row = session.get(NodeUpgradeGate, gate_row.id)
        if gate_row is None:
            raise HTTPException(
                status_code=400, detail=f"Node {host!r} không còn dữ liệu NodeUpgradeGate — thử lại"
            )
        # Code-review fix (TOCTOU): the blocking SSH re-check above closed
        # the first session and can take a while — re-verify the gate is
        # STILL exactly PREPARED right before mutating it, since a
        # concurrent double-click or a concurrent Abort could have changed
        # it during that window.
        if gate_row.state != NodeUpgradeGateState.PREPARED.value:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Node {host!r} không còn ở trạng thái 'Sẵn sàng cài lại OS' (hiện tại: "
                    f"{gate_row.state}) — có thể một thao tác khác đã chạy song song, thử tải lại trang"
                ),
            )
        # roles_snapshot is authoritative (NOT a live configured_nodes()
        # lookup) — same reasoning Story 11.3 gives for Abort, with even
        # more force here since Prepare→Confirm also spans the physical OS
        # reinstall, the longest gap in the whole flow.
        roles = json.loads(gate_row.roles_snapshot or "[]")

        incident = Incident(
            ceph_code=NODE_OS_GATE_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=f"Xác nhận & Phục hồi node {host} (mục tiêu Ceph {target_version}) bởi {user}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()

        action_params = {
            "host": host,
            "target_version": target_version,
            "roles": roles,
            "nodes": [host],
            "node_upgrade_gate_id": gate_row.id,
        }
        action = Action(
            incident_id=incident.id,
            action_id=NODE_OS_GATE_RECOVER_ACTION_ID,
            classification=gate.classify_action(NODE_OS_GATE_RECOVER_ACTION_ID).value,  # always RISKY
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=(
                f"Xác nhận & Phục hồi node {host} (vai: {', '.join(roles)}) lên Ceph {target_version}"
            ),
            target_nodes=json.dumps([host]),
        )
        session.add(action)
        session.flush()

        action_params["action_pk"] = action.id
        action_params["incident_id"] = incident.id
        action.action_params = json.dumps(action_params)
        action.proposed_command = await asyncio.to_thread(
            _safe_command_preview, NODE_OS_GATE_RECOVER_ACTION_ID, host, action_params
        )

        gate_row.confirm_action_id = action.id
        gate_row.state = NodeUpgradeGateState.RECOVERING.value

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()
        action_id_for_approval = action.id

    try:
        await asyncio.to_thread(approve_action_core, action_id_for_approval, user)
    except ActionConflictError as exc:
        logger.error(
            "confirm_node_os_gate: approve_action_core unexpectedly raised ActionConflictError for "
            "its own freshly-created confirm action %s: %s",
            action_id_for_approval,
            exc.detail,
        )
        raise HTTPException(status_code=409, detail=exc.detail)

    return RedirectResponse(url=f"/upgrade/gate?target_version={target_version}", status_code=303)


@router.post("/upgrade/propose-package-download")
async def propose_package_download_upgrade(
    request: Request,
    target_version: str = Form(...),
    user: str = Depends(require_login),
):
    """ceph-deploy path, option 1 — download the target release from
    download.ceph.com on each configured node (mon/mgr/osd/rgw, in that
    priority order — shared/cluster_nodes.py::configured_nodes) and restart
    whatever daemons that node runs. See worker/executor/commands.py's
    `_upgrade_ceph_cluster_package_download_command` for the actual
    per-host command, and this module's `_PACKAGE_METHOD_SAFETY_NOTE` for
    why this has no cephadm-style orchestrator gating between nodes.

    Story 11.1 (OS Upgrade Gate, AD-20): if one or more target nodes fail
    the OS-compatibility preflight, this now renders the dedicated
    `os_upgrade_gate.html` screen (200) instead of raising
    HTTPException(400) — no Incident/Action is created either way (the
    `with db.SessionLocal()` block below is never committed on this path),
    matching the ad-hoc behavior this replaces exactly except for what the
    operator sees.

    Story 11.5 (FR-57, integration verification only, zero code change
    here): once every gated node's `NodeUpgradeGate.state` reaches `DONE`
    (Story 11.4), `_check_os_upgrade_needed` naturally returns `[]` again
    (it re-checks live, doesn't consult `NodeUpgradeGate` at all) and this
    route falls straight through to the normal proposal flow below —
    resuming the cluster upgrade is NOT automatic, the operator must submit
    this form again themselves.
    """
    target_version = target_version.strip()
    cluster = _selected_cluster(request)
    if not _TARGET_VERSION_RE.match(target_version):
        raise HTTPException(status_code=400, detail="Phiên bản không hợp lệ (định dạng x.y.z, vd 19.2.0)")
    if _effective_exec_mode(cluster) != "none":
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
        _reject_duplicate_proposal(session, cluster)

        target_nodes = [n["host"] for n in _effective_nodes(cluster)]
        if not target_nodes:
            raise HTTPException(status_code=400, detail="Chưa cấu hình node nào (xem trang Cài đặt)")

        incompatible_nodes = await asyncio.to_thread(_check_os_upgrade_needed, target_version, target_nodes)
        if incompatible_nodes:
            context = await _build_os_upgrade_gate_context(
                session, user, target_version, codename.capitalize(), incompatible_nodes
            )
            return templates.TemplateResponse(request, "os_upgrade_gate.html", context)

        action_params = {"target_version": target_version}
        if not cluster.is_default:
            action_params["_cluster_exec_mode"] = cluster.ceph_exec_mode
        preview_command = await asyncio.to_thread(
            _safe_command_preview, PACKAGE_DOWNLOAD_ACTION_ID, target_nodes[0], action_params
        )
        incident = Incident(
            cluster_id=None if cluster.is_default else cluster.id,
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

    return RedirectResponse(url=_upgrade_url(cluster), status_code=303)


@router.post("/upgrade/propose-package-local")
async def propose_package_local_upgrade(
    request: Request,
    package_dir: str = Form(...),
    user: str = Depends(require_login),
):
    """ceph-deploy path, option 2 — install from a directory of packages
    already staged on each node (no download, no scp — the operator is
    responsible for having put the right packages at the SAME path on
    every configured node beforehand)."""
    package_dir = package_dir.strip()
    cluster = _selected_cluster(request)
    if not _PACKAGE_DIR_RE.match(package_dir):
        raise HTTPException(
            status_code=400,
            detail="Đường dẫn không hợp lệ (phải là đường dẫn tuyệt đối, vd /opt/ceph-packages)",
        )
    if _effective_exec_mode(cluster) != "none":
        raise HTTPException(
            status_code=400,
            detail="Chỉ áp dụng cho cụm kiểu ceph-deploy (ceph_exec_mode=none)",
        )

    with db.SessionLocal() as session:
        _reject_duplicate_proposal(session, cluster)

        target_nodes = [n["host"] for n in _effective_nodes(cluster)]
        if not target_nodes:
            raise HTTPException(status_code=400, detail="Chưa cấu hình node nào (xem trang Cài đặt)")

        action_params = {"package_dir": package_dir}
        if not cluster.is_default:
            action_params["_cluster_exec_mode"] = cluster.ceph_exec_mode
        preview_command = await asyncio.to_thread(
            _safe_command_preview, PACKAGE_LOCAL_ACTION_ID, target_nodes[0], action_params
        )
        incident = Incident(
            cluster_id=None if cluster.is_default else cluster.id,
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

    return RedirectResponse(url=_upgrade_url(cluster), status_code=303)


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
async def pause_upgrade_route(request: Request, user: str = Depends(require_login)):
    cluster = _selected_cluster(request)
    try:
        pause_upgrade() if cluster.is_default else pause_upgrade_with(*_cluster_connection(cluster))
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không tạm dừng được: {exc}")
    with db.SessionLocal() as session:
        _, incident = _latest_upgrade_action(session, cluster)
        if incident is not None:
            audit.record(
                session,
                incident_id=incident.id,
                action_id=None,
                event_type=audit.EVENT_CLUSTER_UPGRADE_PAUSED,
                actor=user,
            )
            session.commit()
    return RedirectResponse(url=_upgrade_url(cluster), status_code=303)


@router.post("/upgrade/resume")
async def resume_upgrade_route(request: Request, user: str = Depends(require_login)):
    cluster = _selected_cluster(request)
    try:
        resume_upgrade() if cluster.is_default else resume_upgrade_with(*_cluster_connection(cluster))
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không tiếp tục được: {exc}")
    with db.SessionLocal() as session:
        _, incident = _latest_upgrade_action(session, cluster)
        if incident is not None:
            audit.record(
                session,
                incident_id=incident.id,
                action_id=None,
                event_type=audit.EVENT_CLUSTER_UPGRADE_RESUMED,
                actor=user,
            )
            session.commit()
    return RedirectResponse(url=_upgrade_url(cluster), status_code=303)


@router.post("/upgrade/unset-osd-flags")
async def unset_upgrade_osd_flags_route(request: Request, user: str = Depends(require_login)):
    """Manual cleanup for the cephadm path — worker/executor/commands.py::
    _upgrade_ceph_cluster_command sets noout/noscrub/nodeep-scrub/
    nosnaptrim before starting `ceph orch upgrade start`, but can't unset
    them itself (fire-and-forget; the real upgrade continues asynchronously
    in cephadm's own mgr module long after that command returns — see that
    posture as pause/resume just above: an operator explicitly clicking
    this once they see (via this page's own live status, `get_upgrade_
    status()`) that the upgrade has actually finished."""
    cluster = _selected_cluster(request)
    try:
        (unset_upgrade_osd_flags() if cluster.is_default else
         unset_upgrade_osd_flags_with(*_cluster_connection(cluster)))
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không bỏ được các cờ noout/noscrub: {exc}")
    with db.SessionLocal() as session:
        _, incident = _latest_upgrade_action(session, cluster)
        if incident is not None:
            audit.record(
                session,
                incident_id=incident.id,
                action_id=None,
                event_type=audit.EVENT_CLUSTER_UPGRADE_OSD_FLAGS_UNSET,
                actor=user,
            )
            session.commit()
    return RedirectResponse(url=_upgrade_url(cluster), status_code=303)
