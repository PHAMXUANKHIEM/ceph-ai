"""Two-way Telegram bridge for the Dashboard Chatbox AI.

The bridge deliberately has its own Bot Token/Chat ID pair.  Incoming
messages are accepted only from that exact configured chat; alert channels
remain notification/approval channels.  ``single`` reuses the normal
Dashboard ``run_chat_turn`` path and ``dual`` reuses the same bounded
Planner/Implementer exchange used by the web chatbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import httpx
import json
import logging
import queue
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

from config.settings import settings
from dashboard.chat_client import ChatTurnError, MAX_HISTORY_MESSAGES, run_chat_turn
from dashboard.dual_ai_chat import (
    DualAIChatBusy,
    DualAIChatError,
    DualAIChatExhausted,
    run_single_full_access_chat,
    stream_dual_ai_chat,
)
from dashboard.routes.actions import ApprovalOutcome, approve_action_core
from dashboard.routes.chat import _confirm_chat_action_core
from shared import db
from shared import telegram_federation
from shared.full_executor_auth import executor_token
from shared.codex_app_server import (
    CodexAppServerError,
    refresh_app_server_after_cli_login,
    start_cli_device_login,
)
from shared.models import Action, ActionStatus, ChatMessage, Cluster
from shared.single_full_scope import normalize_scope, sign_scope
from shared.telegram_client import edit_telegram_message, send_telegram_message, send_telegram_message_with_keyboard

logger = logging.getLogger(__name__)

CHAT_CONFIRM_PREFIX = "chatconfirm:"
CHAT_APPROVE_PREFIX = "chatapprove:"
DUAL_STOP_PREFIX = "dualstop:"
QUOTA_LOGIN_PREFIX = "quotalogin:"
AI_MODE_PREFIX = "aimode:"
CLUSTER_SELECT_PREFIX = "clusterselect:"
_TELEGRAM_ACTOR_PREFIX = "telegram-chat:"
_MAX_MESSAGE_CHARS = 12000
_MESSAGE_QUEUE_SIZE = 4
_FULL_CONFIRM_TTL_SECONDS = 300
_CODEX_LOGIN_WATCH_TIMEOUT_SECONDS = 600
_DESTRUCTIVE_CONFIRM_TTL_SECONDS = 300
_CLUSTER_LOOKUP_TIMEOUT_SECONDS = 8
_SERVICE_OR_CEPH_CHANGE_RE = re.compile(
    r"(?i)(?:"
    r"\brestart\b|\breboot\b|\bshutdown\b|\bpoweroff\b|\bstop\b"
    r"|\bsystemctl\s+(?:restart|stop|reboot|poweroff)\b"
    r"|\bceph\s+(?:orch\s+daemon\s+restart|osd\s+(?:out|in|fail)|mon\s+(?:remove|rm))\b"
    r"|khởi\s+động\s+lại|tắt\s+(?:dịch\s+vụ|osd|mon|mgr)|dừng\s+(?:dịch\s+vụ|osd|mon|mgr)"
    r")"
)
_DIRECT_DATA_DESTRUCTION_RE = re.compile(
    r"(?i)(?:"
    r"\brm\s+-[^\n]*r|\bmkfs(?:\.|\s)|\bwipefs\b|\bdd\b[^\n]*\bof=/dev/"
    r"|\bceph\s+(?:osd\s+(?:purge|destroy)|fs\s+rm|volume\s+rm)\b"
    r"|\brbd\s+rm\b|\b(?:lv|pv|vg)remove\b|\bzpool\s+destroy\b|\bdrop\s+database\b"
    r"|\bdelete\s+(?:pool|osd|volume|database|data)\b"
    r"|(?:x[oó]a|xoá)\s+(?:pool|osd|volume|database|dữ\s+liệu|ổ\s+đĩa)\b"
    r")"
)
_FULL_RUN_STATE_PATH = Path("/var/lib/ceph-ai/telegram-single-full-runs.json")
_MODE_STATE_PATH = Path("/var/lib/ceph-ai/telegram-chat-modes.json")
_CLUSTER_STATE_PATH = Path("/var/lib/ceph-ai/telegram-chat-clusters.json")
_CONFIRM_STATE_PATH = Path("/var/lib/ceph-ai/telegram-single-full-confirmations.json")
_HELP_TEXT = (
    "Chatbox AI Telegram đã sẵn sàng.\n\n"
    "/model — Chọn 1 AI hoặc 2 AI\n"
    "/single — Chọn 1 AI\n"
    "/dual — Chọn 2 AI trao đổi và sửa code trong workspace cô lập\n"
    "/single_full — 1 AI có toàn quyền source và server (chỉ user được cấp riêng)\n"
    "/confirm_full <mã> — Xác nhận một phiên Single Full\n"
    "/confirm_destructive <mã> — Xác nhận yêu cầu có thể tác động service/dữ liệu\n"
    "/stop — Dừng phiên Hai AI đang chạy\n"
    "/status — Xem phiên AI và xác nhận đang chờ\n"
    "/cluster — Chọn cụm Ceph cho phiên chat\n"
    "/ask <nội dung> — Gửi câu hỏi trong group khi Privacy Mode đang bật\n"
    "/new — Bắt đầu đoạn chat mới\n"
    "/help — Xem trợ giúp\n\n"
    "Bạn cũng có thể dùng /model single, /model dual hoặc /model single_full."
)
_mode_by_chat: dict[str, str] = {}
_session_by_chat: dict[str, str] = {}
_cluster_by_chat: dict[str, str] = {}
_message_queue: queue.Queue[tuple[dict, str]] = queue.Queue(maxsize=_MESSAGE_QUEUE_SIZE)
_message_worker_lock = threading.Lock()
_message_worker_started = False
_dashboard_loop_lock = threading.Lock()
_dashboard_loop: asyncio.AbstractEventLoop | None = None
_dual_runs_lock = threading.Lock()
_dual_runs: dict[str, dict] = {}
_full_runs_lock = threading.Lock()
_full_runs: dict[str, dict] = {}
_full_run_state_file_lock = threading.Lock()
_mode_state_file_lock = threading.Lock()
_cluster_state_file_lock = threading.Lock()
_confirm_state_file_lock = threading.Lock()
_codex_login_watchers: dict[str, asyncio.Task] = {}
_NO_CLUSTER_OVERRIDE = object()


def set_dashboard_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Route Telegram AI work through Dashboard's loop.

    Codex's app-server is an asyncio subprocess shared with web Chatbox, so
    Telegram must not create a second loop in its worker thread.
    """
    global _dashboard_loop
    with _dashboard_loop_lock:
        _dashboard_loop = loop


def clear_dashboard_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    global _dashboard_loop
    with _dashboard_loop_lock:
        if loop is None or _dashboard_loop is loop:
            _dashboard_loop = None


def _request_stop(chat_id: str, actor: str):
    """Mark the newest active dual run for a chat and cancel its task."""
    with _dual_runs_lock:
        matching = [
            state for state in _dual_runs.values()
            if state.get("chat_id") == chat_id and state.get("actor") == actor
        ]
        if not matching:
            return None
        state = matching[-1]
        state["stop_requested"] = True
        task = state.get("task")
        loop = state.get("loop")
        status_message_id = state.get("status_message_id")
        if status_message_id is not None and task is not None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(task.cancel)
        return state


def _request_full_stop(chat_id: str, actor: str):
    """Cancel the active Single Full task for exactly this Telegram actor."""
    with _full_runs_lock:
        matching = [
            state for state in _full_runs.values()
            if state.get("chat_id") == chat_id and state.get("actor") == actor
        ]
        if not matching:
            return None
        state = matching[-1]
        state["stop_requested"] = True
        state["stage"] = "đang dừng"
        task = state.get("task")
        loop = state.get("loop")
        if task is not None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(task.cancel)
        return state


def handle_stop_message(message: dict, bot_token: str) -> bool:
    """Handle /stop in the polling thread, bypassing the busy AI worker."""
    if not is_allowed_message(message, bot_token):
        return False
    parts = str(message.get("text") or "").strip().lower().split()
    name = parts[0].split("@", 1)[0] if parts else ""
    if name != "/stop":
        return False
    chat_id = _chat_id(message)
    state = _request_stop(chat_id, _actor(message))
    mode = "Hai AI"
    if state is None:
        state = _request_full_stop(chat_id, _actor(message))
        mode = "Single Full"
    status_id = state.get("status_message_id") if state else None
    if status_id is not None:
        try:
            edit_telegram_message(
                bot_token, chat_id, status_id,
                f"⏹ Đã nhận lệnh /stop; đang dừng phiên {mode}...",
            )
        except Exception:
            logger.exception("telegram_chat: failed to update status after /stop")
    text = f"Đã nhận /stop, đang dừng phiên {mode}." if state else "Hiện không có phiên AI đang chạy."
    try:
        send_telegram_message(bot_token, chat_id, text)
    except Exception:
        logger.exception("telegram_chat: failed to report /stop result")
    return True


def _duration_text(started_at: float | None) -> str:
    seconds = max(0, int(time.monotonic() - started_at)) if started_at else 0
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _status_text(chat_id: str, actor: str) -> str:
    """Build a per-operator status without exposing another user's session."""
    lines = ["📊 TRẠNG THÁI CHATBOX AI"]
    with _full_runs_lock:
        full_runs = [
            dict(state) for state in _full_runs.values()
            if state.get("chat_id") == chat_id and state.get("actor") == actor
        ]
    with _dual_runs_lock:
        dual_runs = [
            dict(state) for state in _dual_runs.values()
            if state.get("chat_id") == chat_id and state.get("actor") == actor
        ]
    if full_runs:
        run = full_runs[-1]
        lines.append(f"• Single Full: {run.get('stage', 'đang chạy')} · {_duration_text(run.get('started_at'))}")
    if dual_runs:
        run = dual_runs[-1]
        lines.append(f"• Hai AI: {run.get('stage', 'đang chạy')} · {_duration_text(run.get('started_at'))}")
    if not full_runs and not dual_runs:
        lines.append("• Không có phiên AI đang chạy.")
    with _confirm_state_file_lock:
        confirmations = _load_confirmations()
    full_pending = confirmations["full"].get(actor)
    destructive_pending = confirmations["destructive"].get(actor)
    if full_pending and full_pending.get("chat_id") == chat_id:
        remaining = max(0, int(full_pending.get("expires_at", 0) - time.time()))
        lines.append(f"• Chờ /confirm_full · còn {remaining // 60}m {remaining % 60:02d}s.")
    if destructive_pending and destructive_pending.get("chat_id") == chat_id:
        remaining = max(0, int(destructive_pending.get("expires_at", 0) - time.time()))
        lines.append(f"• Chờ /confirm_destructive · còn {remaining // 60}m {remaining % 60:02d}s.")
    lines.append(f"• Chế độ đã chọn: {_mode(actor)}.")
    return "\n".join(lines)


def handle_status_message(message: dict, bot_token: str) -> bool:
    """Handle /status in the poller so it works while an AI job is busy."""
    if not is_allowed_message(message, bot_token):
        return False
    parts = str(message.get("text") or "").strip().lower().split()
    name = parts[0].split("@", 1)[0] if parts else ""
    if name != "/status":
        return False
    chat_id = _chat_id(message)
    try:
        send_telegram_message(bot_token, chat_id, _status_text(chat_id, _actor(message)))
    except Exception:
        logger.exception("telegram_chat: failed to report /status")
    return True


def configured() -> tuple[str, str] | None:
    """Return the enabled Chatbox bot destination, or ``None``."""
    token = str(getattr(settings, "telegram_chatbox_bot_token", "") or "").strip()
    chat_id = str(getattr(settings, "telegram_chatbox_chat_id", "") or "").strip()
    if not token or not chat_id or not getattr(settings, "telegram_chatbox_enabled", True):
        return None
    return token, chat_id


def configured_token() -> str | None:
    target = configured()
    return target[0] if target else None


def _allowed_user_ids() -> set[str] | None:
    """Return configured sender ids; ``None`` means malformed config."""
    raw = str(getattr(settings, "telegram_chatbox_allowed_user_ids", "") or "").strip()
    if not raw:
        return set()
    values = {item.strip() for item in raw.split(",") if item.strip()}
    if not values or any(not item.isdigit() for item in values):
        return None
    return values


def _full_access_user_ids() -> set[str] | None:
    """Return the stricter /single-full sender allow-list; empty is deny."""
    raw = str(getattr(settings, "telegram_chatbox_full_access_user_ids", "") or "").strip()
    if not raw:
        return set()
    values = {item.strip() for item in raw.split(",") if item.strip()}
    if not values or any(not item.isdigit() for item in values):
        return None
    return values


def _sender_can_use_full_access(update: dict) -> bool:
    allowed_ids = _full_access_user_ids()
    if not allowed_ids:
        return False
    sender_id = str((update.get("from") or {}).get("id", ""))
    return sender_id in allowed_ids


def _full_run_token_key(bot_token: str) -> str:
    return hashlib.sha256(bot_token.encode()).hexdigest()


def _load_persisted_modes() -> dict[str, str]:
    try:
        payload = json.loads(_MODE_STATE_PATH.read_text())
        if not isinstance(payload, dict):
            return {}
        return {
            str(actor): str(mode) for actor, mode in payload.items()
            if str(mode) in {"single", "dual", "single-full"}
        }
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("telegram_chat: failed to read persisted chat modes")
        return {}


def _set_mode(actor: str, mode: str) -> None:
    """Persist the selected mode so a Dashboard restart cannot downgrade it."""
    if mode not in {"single", "dual", "single-full"}:
        raise ValueError(f"unsupported Telegram chat mode: {mode}")
    with _mode_state_file_lock:
        modes = _load_persisted_modes()
        modes[actor] = mode
        _MODE_STATE_PATH.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = _MODE_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(modes, sort_keys=True))
        temporary.chmod(0o600)
        temporary.replace(_MODE_STATE_PATH)
        _mode_by_chat[actor] = mode


def _mode(actor: str) -> str:
    current = _mode_by_chat.get(actor)
    if current in {"single", "dual", "single-full"}:
        return current
    with _mode_state_file_lock:
        current = _load_persisted_modes().get(actor, "single")
        _mode_by_chat[actor] = current
        return current


def _load_full_run_markers() -> dict[str, dict]:
    try:
        payload = json.loads(_FULL_RUN_STATE_PATH.read_text())
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("telegram_chat: failed to read Single Full recovery markers")
        return {}


def _write_full_run_markers(markers: dict[str, dict]) -> None:
    _FULL_RUN_STATE_PATH.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = _FULL_RUN_STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(markers, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(_FULL_RUN_STATE_PATH)


def _mark_full_run_started(
    run_id: str, bot_token: str, chat_id: str, actor: str, *,
    cluster_ref: str | None = None, cluster_name: str | None = None,
) -> None:
    """Record a full-access execution before its CLI process can start."""
    with _full_run_state_file_lock:
        markers = _load_full_run_markers()
        markers[run_id] = {
            "token_key": _full_run_token_key(bot_token),
            "chat_id": chat_id,
            "actor": actor,
            "cluster_ref": cluster_ref or "",
            "cluster_name": cluster_name or "",
            "started_at": time.time(),
        }
        _write_full_run_markers(markers)


def _mark_full_run_finished(run_id: str) -> None:
    with _full_run_state_file_lock:
        markers = _load_full_run_markers()
        if run_id in markers:
            markers.pop(run_id, None)
            _write_full_run_markers(markers)


def report_interrupted_full_runs(bot_token: str) -> None:
    """Notify the owner after restart; never rerun an unrestricted task."""
    token_key = _full_run_token_key(bot_token)
    with _full_run_state_file_lock:
        markers = _load_full_run_markers()
        interrupted = [
            item for item in markers.values()
            if isinstance(item, dict) and item.get("token_key") == token_key
        ]
        if not interrupted:
            return
        remaining = {
            run_id: item for run_id, item in markers.items()
            if not isinstance(item, dict) or item.get("token_key") != token_key
        }
        _write_full_run_markers(remaining)
    for item in interrupted:
        chat_id = str(item.get("chat_id") or "")
        if not chat_id:
            continue
        try:
            send_telegram_message(
                bot_token,
                chat_id,
                "⚠️ Dashboard đã khởi động lại khi Single Full đang chạy. "
                "Lệnh không được tự chạy lại; hãy kiểm tra server rồi gửi yêu cầu mới nếu cần.",
            )
        except Exception:
            logger.exception("telegram_chat: failed to report interrupted Single Full run")


def _load_confirmations() -> dict[str, dict]:
    """Read pending Single Full / destructive confirmations from disk.

    These used to live only in a module-level dict, so a Dashboard restart
    between issuing a confirmation code and the user's ``/confirm_full``
    reply silently dropped an otherwise-valid, unexpired code (the user saw
    "không hợp lệ hoặc đã hết hạn" despite replying immediately with the
    right code). Persisting them the same way ``_FULL_RUN_STATE_PATH`` and
    ``_MODE_STATE_PATH`` already do fixes that.
    """
    try:
        payload = json.loads(_CONFIRM_STATE_PATH.read_text())
    except FileNotFoundError:
        payload = None
    except Exception:
        logger.exception("telegram_chat: failed to read persisted Single Full confirmations")
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    full = payload.get("full")
    destructive = payload.get("destructive")
    return {
        "full": full if isinstance(full, dict) else {},
        "destructive": destructive if isinstance(destructive, dict) else {},
    }


def _write_confirmations(confirmations: dict[str, dict]) -> None:
    _CONFIRM_STATE_PATH.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    temporary = _CONFIRM_STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(confirmations, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(_CONFIRM_STATE_PATH)


def _clear_full_confirmation(actor: str) -> None:
    with _confirm_state_file_lock:
        confirmations = _load_confirmations()
        confirmations["full"].pop(actor, None)
        confirmations["destructive"].pop(actor, None)
        _write_confirmations(confirmations)


def _issue_full_confirmation(
    actor: str, chat_id: str, prompt: str, *, cluster_ref: str,
) -> str:
    """Store one short-lived, exact-task confirmation for a full run."""
    confirmation = secrets.token_urlsafe(8)
    with _confirm_state_file_lock:
        confirmations = _load_confirmations()
        confirmations["full"][actor] = {
            "token": confirmation,
            "chat_id": chat_id,
            "prompt": prompt,
            "cluster_ref": cluster_ref,
            "expires_at": time.time() + _FULL_CONFIRM_TTL_SECONDS,
        }
        _write_confirmations(confirmations)
    return confirmation


async def _consume_full_confirmation(
    token: str, chat_id: str, actor: str, text: str, *, full_access_allowed: bool,
) -> tuple[bool, str | None, str | None]:
    """Return the exact confirmed request; never accept a broad OK token."""
    parts = text.strip().split()
    name = parts[0].split("@", 1)[0].lower() if parts else ""
    if name != "/confirm_full":
        return False, None, None
    if not full_access_allowed:
        await _send(token, chat_id, "Single Full chưa được cấp cho Telegram user này.")
        return True, None, None
    supplied = parts[1] if len(parts) == 2 else ""
    with _confirm_state_file_lock:
        confirmations = _load_confirmations()
        pending = confirmations["full"].get(actor)
        if (
            pending is None
            or pending.get("chat_id") != chat_id
            or time.time() > pending.get("expires_at", 0)
            or not secrets.compare_digest(supplied, str(pending.get("token") or ""))
        ):
            pending = None
        confirmations["full"].pop(actor, None)
        _write_confirmations(confirmations)
    if pending is None:
        await _send(token, chat_id, "Mã xác nhận Single Full không hợp lệ hoặc đã hết hạn. Hãy gửi lại yêu cầu.")
        return True, None, None
    cluster_ref = str(pending.get("cluster_ref") or "").strip()
    if not cluster_ref:
        await _send(token, chat_id, "Mã xác nhận cũ không có scope cụm; hãy gửi lại yêu cầu.")
        return True, None, None
    return True, str(pending["prompt"]), cluster_ref


def _requires_destructive_confirmation(prompt: str) -> bool:
    """Conservatively identify requests that can change or interrupt service."""
    return bool(_SERVICE_OR_CEPH_CHANGE_RE.search(prompt or ""))


def _is_direct_data_destruction(prompt: str) -> bool:
    """Direct destructive server/data operations are never delegated to AI."""
    return bool(_DIRECT_DATA_DESTRUCTION_RE.search(prompt or ""))


def _issue_destructive_confirmation(
    actor: str, chat_id: str, prompt: str, *, cluster_ref: str,
) -> str:
    confirmation = secrets.token_urlsafe(8)
    with _confirm_state_file_lock:
        confirmations = _load_confirmations()
        confirmations["destructive"][actor] = {
            "token": confirmation,
            "chat_id": chat_id,
            "prompt": prompt,
            "cluster_ref": cluster_ref,
            "expires_at": time.time() + _DESTRUCTIVE_CONFIRM_TTL_SECONDS,
        }
        _write_confirmations(confirmations)
    return confirmation


async def _consume_destructive_confirmation(
    token: str, chat_id: str, actor: str, text: str, *, full_access_allowed: bool,
) -> tuple[bool, str | None, str | None]:
    parts = text.strip().split()
    name = parts[0].split("@", 1)[0].lower() if parts else ""
    if name != "/confirm_destructive":
        return False, None, None
    if not full_access_allowed:
        await _send(token, chat_id, "Single Full chưa được cấp cho Telegram user này.")
        return True, None, None
    supplied = parts[1] if len(parts) == 2 else ""
    with _confirm_state_file_lock:
        confirmations = _load_confirmations()
        pending = confirmations["destructive"].get(actor)
        if (
            pending is None
            or pending.get("chat_id") != chat_id
            or time.time() > pending.get("expires_at", 0)
            or not secrets.compare_digest(supplied, str(pending.get("token") or ""))
        ):
            pending = None
        confirmations["destructive"].pop(actor, None)
        _write_confirmations(confirmations)
    if pending is None:
        await _send(token, chat_id, "Mã xác nhận lệnh nguy hiểm không hợp lệ hoặc đã hết hạn.")
        return True, None, None
    cluster_ref = str(pending.get("cluster_ref") or "").strip()
    if not cluster_ref:
        await _send(token, chat_id, "Mã xác nhận cũ không có scope cụm; hãy gửi lại yêu cầu.")
        return True, None, None
    return True, str(pending["prompt"]), cluster_ref


def _sender_allowed(update: dict, *, chat_type: str) -> bool:
    allowed_ids = _allowed_user_ids()
    if allowed_ids is None:
        return False
    sender_id = str((update.get("from") or {}).get("id", ""))
    if allowed_ids:
        return sender_id in allowed_ids
    # A configured private chat is already one-user scoped. Group chats must
    # explicitly list sender IDs before they can invoke admin-level tooling.
    return chat_type == "private"


def is_allowed_message(message: dict, bot_token: str) -> bool:
    target = configured()
    if target is None or target[0] != bot_token:
        return False
    chat = message.get("chat") or {}
    incoming_chat_id = str(chat.get("id", ""))
    return incoming_chat_id == target[1] and _sender_allowed(message, chat_type=str(chat.get("type") or ""))


def is_allowed_callback(callback_query: dict, bot_token: str) -> bool:
    target = configured()
    if target is None or target[0] != bot_token:
        return False
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    incoming_chat_id = str(chat.get("id", ""))
    return incoming_chat_id == target[1] and _sender_allowed(
        callback_query, chat_type=str(chat.get("type") or "")
    )


def _chat_id(message: dict) -> str:
    return str((message.get("chat") or {}).get("id", ""))


def _actor(update: dict) -> str:
    """Stable per-person identity for history and audit events.

    A group chat is an access boundary, not an operator identity: multiple
    allow-listed people must neither share a pending `OK` nor be recorded as
    the same actor. Telegram user ids fit the existing 32-character column.
    """
    sender_id = str((update.get("from") or {}).get("id", "")).strip()
    return f"{_TELEGRAM_ACTOR_PREFIX}{sender_id}"[:32]


def _model_actor() -> str:
    # Configuring this admin-only channel is the trust boundary for the
    # Dashboard's Ceph tools.  Keep the same admin/tool scope as the root
    # Dashboard chatbox while storing the transcript under its Telegram actor.
    return settings.dashboard_username


def _cluster() -> Cluster | None:
    """Return the configured local default for legacy/test call sites."""
    with db.SessionLocal() as session:
        cluster = session.query(Cluster).filter(Cluster.is_default.is_(True)).first()
        if cluster is None or not cluster.is_active:
            return None
        session.expunge(cluster)
        return cluster


def _active_clusters() -> list[dict[str, str | bool]]:
    """Return active Ceph clusters from every Telegram database source."""
    return [
        {
            "id": target.qualified_id,
            "name": target.name,
            "is_default": target.is_default and target.source.key == "local",
        }
        for target in telegram_federation.active_clusters()
    ]


def _load_persisted_cluster_choices() -> dict[str, str]:
    try:
        payload = json.loads(_CLUSTER_STATE_PATH.read_text())
        if not isinstance(payload, dict):
            return {}
        return {str(actor): str(cluster_id) for actor, cluster_id in payload.items() if str(cluster_id)}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("telegram_chat: failed to read persisted chat clusters")
        return {}


def _selected_cluster_id(actor: str) -> str | None:
    current = _cluster_by_chat.get(actor)
    if current:
        return current
    with _cluster_state_file_lock:
        current = _load_persisted_cluster_choices().get(actor)
        if current:
            _cluster_by_chat[actor] = current
        return current


def _set_cluster(actor: str, cluster_id: str) -> None:
    """Persist a Telegram user's cluster and start a fresh cluster-scoped session."""
    cluster_id = str(cluster_id).strip()
    if not cluster_id:
        raise ValueError("cluster_id must not be empty")
    with _cluster_state_file_lock:
        choices = _load_persisted_cluster_choices()
        choices[actor] = cluster_id
        _CLUSTER_STATE_PATH.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = _CLUSTER_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(choices, sort_keys=True))
        temporary.chmod(0o600)
        temporary.replace(_CLUSTER_STATE_PATH)
        _cluster_by_chat[actor] = cluster_id
    # Never mix history or pending node-command confirmations between clusters.
    _session_by_chat.pop(actor, None)


def _clear_cluster(actor: str) -> None:
    """Require an explicit cluster choice after every mode change."""
    with _cluster_state_file_lock:
        choices = _load_persisted_cluster_choices()
        choices.pop(actor, None)
        _CLUSTER_STATE_PATH.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = _CLUSTER_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(choices, sort_keys=True))
        temporary.chmod(0o600)
        temporary.replace(_CLUSTER_STATE_PATH)
        _cluster_by_chat.pop(actor, None)
    _session_by_chat.pop(actor, None)


def _cluster_for_actor(actor: str) -> Cluster | None:
    resolved = _resolve_cluster_for_actor(actor)
    return resolved[1] if resolved is not None else None


def _cluster_reference(actor: str, cluster: Cluster) -> str:
    """Return the canonical qualified Telegram cluster reference for a run."""
    selected_id = _selected_cluster_id(actor)
    if selected_id:
        resolved = telegram_federation.resolve_cluster(selected_id)
        if resolved is not None:
            return resolved[0].qualified_id
    database_url = db.current_database_url() or settings.database_url
    source = telegram_federation.source_for_url(database_url)
    return f"{source.key}:{cluster.id}" if source is not None else str(cluster.id)


def _single_full_cluster_context(actor: str, cluster: Cluster) -> dict[str, str]:
    """Serialize only the selected cluster's connection scope for the executor."""
    selected_id = _selected_cluster_id(actor)
    resolved = telegram_federation.target_for_actor_cluster(selected_id) if selected_id else None
    if resolved is not None:
        target, resolved_cluster = resolved
        if str(resolved_cluster.id) != str(cluster.id):
            raise DualAIChatError("Single Full bị từ chối: scope cụm đã thay đổi")
        database_source = target.source.key
        database_url = target.source.url
    else:
        # Cluster-specific alert/approval bots run inside an explicit
        # db.use_database() context but do not have a persisted selector.
        database_url = db.current_database_url() or settings.database_url
        source = telegram_federation.source_for_url(database_url)
        database_source = source.key if source is not None else "local"
    return normalize_scope({
        "cluster_id": str(getattr(cluster, "id", "")),
        "cluster_ref": _cluster_reference(actor, cluster),
        "name": str(getattr(cluster, "name", "") or ""),
        "database_source": database_source,
        "database_url": database_url,
        "ceph_mon_nodes": str(getattr(cluster, "ceph_mon_nodes", "") or ""),
        "ceph_mon_hostnames": str(getattr(cluster, "ceph_mon_hostnames", "") or ""),
        "ceph_mgr_nodes": str(getattr(cluster, "ceph_mgr_nodes", "") or ""),
        "ceph_osd_nodes": str(getattr(cluster, "ceph_osd_nodes", "") or ""),
        "ceph_rgw_nodes": str(getattr(cluster, "ceph_rgw_nodes", "") or ""),
        "ceph_exec_mode": str(getattr(cluster, "ceph_exec_mode", "") or ""),
        "ceph_container_name": str(getattr(cluster, "ceph_container_name", "") or ""),
        "ceph_osd_container_name": str(getattr(cluster, "ceph_osd_container_name", "") or ""),
        "ceph_rgw_container_name": str(getattr(cluster, "ceph_rgw_container_name", "") or ""),
        "ssh_user": str(getattr(cluster, "ssh_user", "") or ""),
        "ssh_key_path": str(getattr(cluster, "ssh_key_path", "") or ""),
        "ceph_keyring_path": str(getattr(cluster, "ceph_keyring_path", "") or ""),
    })


def _resolve_cluster_for_actor(actor: str):
    """Return ``(federated target, detached Cluster)`` for this operator."""
    selected_id = _selected_cluster_id(actor)
    if not selected_id:
        return None
    return telegram_federation.target_for_actor_cluster(selected_id)


_MODE_LABELS = {
    "single": "Chat với một AI",
    "dual": "Hai AI trao đổi và sửa code trong workspace cô lập",
    "single-full": "Single Full — toàn quyền source và server",
}


def _cluster_selection_buttons(clusters: list[dict[str, str | bool]]) -> list[tuple[str, str]]:
    buttons = []
    for item in clusters:
        marker = " ⭐" if item["is_default"] else ""
        label = f"{item['name']}{marker}"[:48]
        buttons.append((label, f"{CLUSTER_SELECT_PREFIX}{item['id']}"))
    return buttons


async def _send_cluster_selector(token: str, chat_id: str, mode: str | None = None) -> bool:
    try:
        clusters = await asyncio.wait_for(
            asyncio.to_thread(_active_clusters),
            timeout=_CLUSTER_LOOKUP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("telegram_chat: active cluster lookup timed out")
        await _send(token, chat_id, "DB cụm đang phản hồi chậm; hãy thử lại sau vài giây.")
        return False
    except Exception:
        logger.exception("telegram_chat: failed to load active clusters")
        await _send(token, chat_id, "Không tải được danh sách cụm Ceph; kiểm tra kết nối database.")
        return False
    if not clusters:
        await _send(token, chat_id, "Chưa có cụm Ceph active để chọn.")
        return False
    mode_text = f"\nChế độ: {_MODE_LABELS.get(mode or '', mode or _mode(''))}" if mode else ""
    await asyncio.to_thread(
        send_telegram_message_with_keyboard,
        token,
        chat_id,
        f"🔌 Chọn cụm Ceph để AI kết nối{mode_text}:",
        _cluster_selection_buttons(clusters),
    )
    return True


async def _send_mode_selector(token: str, chat_id: str) -> None:
    await asyncio.to_thread(
        send_telegram_message_with_keyboard,
        token,
        chat_id,
        "🤖 Chọn chế độ AI:",
        [
            ("1️⃣ Một AI", f"{AI_MODE_PREFIX}single"),
            ("2️⃣ Hai AI", f"{AI_MODE_PREFIX}dual"),
            ("🔐 Single Full", f"{AI_MODE_PREFIX}single-full"),
        ],
    )


async def _select_mode(
    token: str,
    chat_id: str,
    actor: str,
    mode: str,
    *,
    full_access_allowed: bool,
) -> str | None:
    if mode not in _MODE_LABELS:
        return None
    if mode == "single-full" and not full_access_allowed:
        await _send(token, chat_id, "Single Full chưa được cấp cho Telegram user này.")
        return None
    if mode != "single-full":
        _clear_full_confirmation(actor)
    try:
        _set_mode(actor, mode)
    except Exception:
        logger.exception("telegram_chat: failed to persist selected mode")
        await _send(token, chat_id, "Không lưu được chế độ AI; chưa chuyển chế độ.")
        return None
    _clear_cluster(actor)
    await _send(token, chat_id, f"Đã chuyển sang chế độ {_MODE_LABELS[mode]}.")
    await _send_cluster_selector(token, chat_id, mode)
    return _MODE_LABELS[mode]


def _history(actor: str, session_id: str, cluster_id: str) -> list[dict]:
    with db.SessionLocal() as session:
        rows = (
            session.query(ChatMessage)
            .filter(
                ChatMessage.session_id == session_id,
                ChatMessage.actor == actor,
                ChatMessage.cluster_id == cluster_id,
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(MAX_HISTORY_MESSAGES)
            .all()
        )
        return [{"role": row.role, "content": row.content} for row in reversed(rows)]


def _session_and_history(actor: str, cluster_id: str) -> tuple[str, list[dict]]:
    session_id = _session_by_chat.get(actor)
    with db.SessionLocal() as session:
        if not session_id:
            latest = (
                session.query(ChatMessage)
                .filter(ChatMessage.actor == actor, ChatMessage.cluster_id == cluster_id)
                .order_by(ChatMessage.created_at.desc())
                .first()
            )
            session_id = latest.session_id if latest and latest.session_id else str(uuid.uuid4())
    _session_by_chat[actor] = session_id
    return session_id, _history(actor, session_id, cluster_id)


def _save_message(
    *,
    session_id: str,
    cluster_id: str,
    actor: str,
    role: str,
    content: str,
    proposal: dict | None = None,
    proposed_status: str | None = None,
) -> ChatMessage:
    with db.SessionLocal() as session:
        message = ChatMessage(
            session_id=session_id,
            cluster_id=cluster_id,
            role=role,
            content=content[:_MAX_MESSAGE_CHARS],
            actor=actor,
            proposed_action_id=proposal.get("action_id") if proposal else None,
            proposed_target_nodes=(json.dumps(proposal.get("target_nodes")) if proposal else None),
            proposed_action_params=(json.dumps(proposal.get("params")) if proposal and proposal.get("params") else None),
            proposed_rationale=proposal.get("rationale") if proposal else None,
            proposed_command_preview=proposal.get("command_preview") if proposal else None,
            proposed_status=proposed_status if proposal else None,
        )
        session.add(message)
        session.commit()
        session.refresh(message)
        return message


def _proposal_text(reply: str, proposal: dict | None) -> str:
    if not proposal:
        return reply
    nodes = ", ".join(proposal.get("target_nodes") or []) or "—"
    return (
        f"{reply}\n\n"
        f"Đề xuất hành động: {proposal.get('action_id', '—')}\n"
        f"Node: {nodes}\n"
        f"Lý do: {proposal.get('rationale') or '—'}\n"
        f"Lệnh xem trước: {proposal.get('command_preview') or 'Không có — cần xử lý thủ công.'}"
    )


async def _send(token: str, chat_id: str, text: str) -> None:
    await asyncio.to_thread(send_telegram_message, token, chat_id, text)


async def _send_proposal(token: str, chat_id: str, text: str, message: ChatMessage) -> None:
    buttons = [("✅ Thực hiện", f"{CHAT_CONFIRM_PREFIX}{telegram_federation.qualify_reference(message.id)}")]
    await asyncio.to_thread(send_telegram_message_with_keyboard, token, chat_id, text, buttons)


def _quota_alert_text(mode: str, exc: DualAIChatExhausted) -> str:
    provider = exc.provider or "AI provider"
    account = f" · tài khoản {exc.account_profile}" if exc.account_profile else ""
    return (
        f"🚨 CẢNH BÁO TOKEN/QUOTA\n"
        f"{mode} đã dừng vì {provider}{account} hết token hoặc quota.\n"
        "Không tự chạy lại yêu cầu. Kiểm tra quota/tài khoản AI rồi gửi yêu cầu mới."
    )


async def _send_quota_alert(
    token: str, chat_id: str, mode: str, exc: DualAIChatExhausted,
) -> None:
    """Report exhausted quota and offer an authorized Codex account switch."""
    text = _quota_alert_text(mode, exc)
    if exc.provider == "codex":
        await asyncio.to_thread(
            send_telegram_message_with_keyboard,
            token,
            chat_id,
            text,
            [("🔑 Đăng nhập Codex khác", f"{QUOTA_LOGIN_PREFIX}codex")],
        )
        return
    await _send(token, chat_id, text)


async def _watch_codex_login_completion(
    *, login_id: str, bot_token: str, chat_id: str,
) -> None:
    """Refresh the live Chatbox client once device authentication completes."""
    deadline = time.monotonic() + _CODEX_LOGIN_WATCH_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            outcome = await refresh_app_server_after_cli_login()
            if outcome == "completed":
                await _send(
                    bot_token,
                    chat_id,
                    "✅ Đã cập nhật tài khoản Codex mới. Gửi yêu cầu AI mới để tiếp tục.",
                )
                return
            if outcome in {"failed", "none"}:
                await _send(
                    bot_token,
                    chat_id,
                    "❌ Đăng nhập Codex không hoàn tất; tài khoản cũ vẫn được giữ nguyên.",
                )
                return
            await asyncio.sleep(2)
        await _send(
            bot_token,
            chat_id,
            "⌛ Phiên đăng nhập Codex chưa hoàn tất. Hãy dùng lại link/mã hoặc bấm nút đăng nhập lại.",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("telegram_chat: failed while waiting for Codex device login")
    finally:
        current = _codex_login_watchers.get(login_id)
        if current is asyncio.current_task():
            _codex_login_watchers.pop(login_id, None)


def _watch_codex_login(*, login_id: str, bot_token: str, chat_id: str) -> None:
    """Start at most one completion watcher for the same device-login flow."""
    current = _codex_login_watchers.get(login_id)
    if current is not None and not current.done():
        return
    _codex_login_watchers[login_id] = asyncio.create_task(
        _watch_codex_login_completion(login_id=login_id, bot_token=bot_token, chat_id=chat_id),
        name=f"telegram-codex-login-{login_id}",
    )


async def _edit_dual_status(token: str, chat_id: str, message_id: int | None, text: str) -> None:
    if message_id is None:
        return
    try:
        await asyncio.to_thread(edit_telegram_message, token, chat_id, message_id, text)
    except Exception:
        logger.exception("telegram_chat: failed to update dual-AI status message")


async def _handle_command(
    token: str, chat_id: str, actor: str, text: str, *, full_access_allowed: bool,
) -> bool:
    command = text.strip().lower().split()
    if not command or not command[0].startswith("/"):
        return False
    name = command[0].split("@", 1)[0]
    if name in {"/help", "/start"}:
        await _send(token, chat_id, _HELP_TEXT)
    elif name == "/status":
        await _send(token, chat_id, _status_text(chat_id, actor))
    elif name in {"/cluster", "/clusters"}:
        await _send_cluster_selector(token, chat_id)
    elif name in {"/single", "/dual", "/single_full", "/single-full"}:
        mode = {
            "/single": "single", "/dual": "dual",
            "/single_full": "single-full", "/single-full": "single-full",
        }[name]
        await _select_mode(
            token, chat_id, actor, mode,
            full_access_allowed=full_access_allowed,
        )
    elif name in {"/model", "/mode"} and len(command) == 2:
        selected = {"1": "single", "2": "dual", "3": "single-full", "single_full": "single-full"}.get(
            command[1], command[1]
        )
        if selected in _MODE_LABELS:
            await _select_mode(
                token, chat_id, actor, selected,
                full_access_allowed=full_access_allowed,
            )
        else:
            await _send(token, chat_id, "Dùng /model single, /model dual hoặc /model single_full.")
    elif name in {"/model", "/mode"}:
        await _send_mode_selector(token, chat_id)
        await _send(token, chat_id, "Hoặc dùng /single, /dual và /single_full.")
    elif name == "/stop":
        state = _request_stop(chat_id, actor)
        mode = "Hai AI"
        if state is None:
            state = _request_full_stop(chat_id, actor)
            mode = "Single Full"
        if state is None:
            await _send(token, chat_id, "Hiện không có phiên AI đang chạy.")
        else:
            if state.get("status_message_id") is not None:
                await _edit_dual_status(
                    token, chat_id, state.get("status_message_id"),
                    f"⏹ Đã nhận lệnh /stop; đang dừng phiên {mode}...",
                )
            await _send(token, chat_id, f"Đã nhận /stop, đang dừng phiên {mode}.")
    elif name == "/new":
        _session_by_chat[actor] = str(uuid.uuid4())
        _clear_full_confirmation(actor)
        await _send(token, chat_id, "Đã bắt đầu đoạn chat mới.")
    else:
        await _send(token, chat_id, "Lệnh không hợp lệ. Gõ /help để xem các lệnh hỗ trợ.")
    return True


async def _run_single_full_in_background(
    *, run_id: str, bot_token: str, chat_id: str, actor: str,
    session_id: str, cluster_id: str, text: str, history: list[dict],
    cluster_context: dict[str, str] | None = None,
) -> None:
    """Keep the Telegram queue responsive while one unrestricted CLI runs."""
    with _full_runs_lock:
        run_state = _full_runs.get(run_id)
        if run_state is None:
            return
        run_state["task"] = asyncio.current_task()
        stopped_before_start = bool(run_state.get("stop_requested"))
        if not stopped_before_start:
            run_state["stage"] = "đang chạy"
    if stopped_before_start:
        # `/stop` may arrive from the polling thread in the tiny window after
        # the run state is registered but before this coroutine gets CPU time.
        # Do not start an unrestricted CLI after acknowledging that stop.
        with _full_runs_lock:
            if _full_runs.get(run_id) is run_state:
                _full_runs.pop(run_id, None)
        return
    try:
        try:
            _mark_full_run_started(
                run_id, bot_token, chat_id, actor,
                cluster_ref=(cluster_context or {}).get("cluster_ref"),
                cluster_name=(cluster_context or {}).get("name"),
            )
        except Exception:
            logger.exception("telegram_chat: cannot persist Single Full recovery marker")
            await _send(
                bot_token, chat_id,
                "Không thể tạo audit recovery cho Single Full; phiên toàn quyền không được khởi chạy.",
            )
            return
        event = await _run_single_full_turn(run_id, text, history, cluster_context)
        content = f"[Single Full · {event.get('provider', '—')}]\n{event.get('content', '')}"
        _save_message(
            session_id=session_id, cluster_id=cluster_id, actor=actor,
            role="assistant", content=content,
        )
        await _send(bot_token, chat_id, content)
    except asyncio.CancelledError:
        await _send(bot_token, chat_id, "⏹ Đã gửi yêu cầu dừng phiên Single Full và đã chờ executor kết thúc.")
        return
    except DualAIChatExhausted as exc:
        await _send_quota_alert(bot_token, chat_id, "Single Full", exc)
    except DualAIChatError as exc:
        await _send(bot_token, chat_id, f"Single Full không thể tiếp tục: {exc}")
    except Exception:
        logger.exception("telegram_chat: single-full mode failed")
        await _send(bot_token, chat_id, "Single Full gặp lỗi nội bộ; kiểm tra log Dashboard.")
    finally:
        try:
            _mark_full_run_finished(run_id)
        except Exception:
            logger.exception("telegram_chat: failed to clear Single Full recovery marker")
        with _full_runs_lock:
            _full_runs.pop(run_id, None)


async def _run_single_full_turn(
    run_id: str, text: str, history: list[dict],
    cluster_context: dict[str, str] | None = None,
) -> dict:
    """Execute Full mode in its dedicated container when configured."""
    if not cluster_context:
        raise DualAIChatError("Single Full bị từ chối: chưa có scope cụm đã chọn")
    try:
        selected_scope = normalize_scope(cluster_context)
    except ValueError as exc:
        raise DualAIChatError(f"Single Full bị từ chối: scope cụm không hợp lệ ({exc})") from exc
    endpoint = str(getattr(settings, "single_full_executor_url", "") or "").rstrip("/")
    token = executor_token()
    if not endpoint:
        return await run_single_full_access_chat(
            text, history, cluster_context=selected_scope,
        )
    if not token:
        raise DualAIChatError("Single Full executor chưa có token xác thực")
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                f"{endpoint}/v1/runs/{run_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "prompt": text,
                    "history": history,
                    "cluster_context": selected_scope,
                    "scope_signature": sign_scope(selected_scope, token),
                },
            )
            response.raise_for_status()
            payload = response.json()
    except asyncio.CancelledError:
        # The HTTP request is shielded inside the executor; explicitly cancel
        # its process tree before reporting `/stop` as complete.
        try:
            # Executor waits up to 10 seconds for the agent process tree, so
            # the caller timeout must be longer than that contract.
            async with httpx.AsyncClient(timeout=12) as client:
                response = await asyncio.shield(client.delete(
                    f"{endpoint}/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token}"},
                ))
                response.raise_for_status()
                payload = response.json()
        except Exception:
            logger.exception("telegram_chat: could not cancel remote Single Full run")
            raise DualAIChatError("Không thể xác nhận executor đã dừng; kiểm tra /status và log.")
        if not isinstance(payload, dict) or payload.get("cancelled") is not True:
            raise DualAIChatError(
                "Executor chưa xác nhận đã dừng; gửi /status để theo dõi trước khi gửi yêu cầu mới."
            )
        raise
    except httpx.HTTPStatusError as exc:
        response = exc.response
        try:
            body = response.json()
        except ValueError:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            code = str(detail.get("code") or "")
            message = str(detail.get("message") or "Single Full executor từ chối yêu cầu")
            if code == "provider_quota_exhausted" or response.status_code == 429:
                raise DualAIChatExhausted(
                    message,
                    provider=detail.get("provider"),
                    account_profile=detail.get("account_profile"),
                ) from exc
            if code == "executor_busy" or response.status_code == 409:
                raise DualAIChatBusy(message) from exc
        else:
            message = str(detail or response.text or "Single Full executor trả lỗi")
        raise DualAIChatError(
            f"Single Full executor lỗi HTTP {response.status_code}: {message}"
        ) from exc
    except httpx.RequestError as exc:
        raise DualAIChatError(f"Không kết nối được Single Full executor: {exc}") from exc
    except httpx.HTTPError as exc:
        raise DualAIChatError(f"Single Full executor gặp lỗi HTTP: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), dict):
        raise DualAIChatError("Single Full executor trả dữ liệu không hợp lệ")
    return payload["event"]


async def _handle_message_impl(
    message: dict, bot_token: str, *, cluster_override: Cluster | None | object = _NO_CLUSTER_OVERRIDE,
) -> None:
    if not is_allowed_message(message, bot_token):
        return
    text = str(message.get("text") or "").strip()
    chat_id = _chat_id(message)
    if not text or not chat_id:
        return
    command_parts = text.split(maxsplit=1)
    command_name = command_parts[0].split("@", 1)[0].lower() if command_parts else ""
    actor = _actor(message)
    confirmed_full = False
    confirmed_destructive = False
    confirmed_cluster_ref: str | None = None
    consumed_confirmation, confirmed_prompt, confirmed_cluster_ref = await _consume_destructive_confirmation(
        bot_token, chat_id, actor, text,
        full_access_allowed=_sender_can_use_full_access(message),
    )
    if consumed_confirmation:
        if confirmed_prompt is None:
            return
        text = confirmed_prompt
        confirmed_full = True
        confirmed_destructive = True
    else:
        consumed_confirmation, confirmed_prompt, confirmed_cluster_ref = await _consume_full_confirmation(
            bot_token, chat_id, actor, text,
            full_access_allowed=_sender_can_use_full_access(message),
        )
        if consumed_confirmation:
            if confirmed_prompt is None:
                return
            text = confirmed_prompt
            confirmed_full = True
    if command_name in {"/ask", "/chat"}:
        if len(command_parts) == 1 or not command_parts[1].strip():
            await _send(bot_token, chat_id, "Dùng /ask <nội dung cần hỏi>.")
            return
        text = command_parts[1].strip()
    elif not confirmed_full and await _handle_command(
        bot_token, chat_id, actor, text,
        full_access_allowed=_sender_can_use_full_access(message),
    ):
        return
    if len(text) > _MAX_MESSAGE_CHARS:
        await _send(bot_token, chat_id, f"Tin nhắn quá dài; giới hạn {_MAX_MESSAGE_CHARS} ký tự.")
        return

    if cluster_override is _NO_CLUSTER_OVERRIDE:
        cluster = await asyncio.to_thread(_cluster_for_actor, actor)
    else:
        cluster = cluster_override
    if cluster is None:
        await _send_cluster_selector(bot_token, chat_id, _mode(actor))
        return
    current_cluster_ref = _cluster_reference(actor, cluster)
    if confirmed_full and confirmed_cluster_ref != current_cluster_ref:
        await _send(
            bot_token,
            chat_id,
            "⛔ Mã xác nhận Single Full thuộc cụm khác hoặc cụm đã thay đổi. "
            "Hãy chọn lại đúng cụm rồi gửi lại yêu cầu.",
        )
        return
    session_id, history = _session_and_history(actor, cluster.id)
    mode = _mode(actor)
    if mode == "single-full" and _is_direct_data_destruction(text):
        _clear_full_confirmation(actor)
        await _send(
            bot_token,
            chat_id,
            "⛔ Single Full không chạy trực tiếp thao tác xoá/phá huỷ dữ liệu. "
            "Hãy dùng workflow Action có review/approval hoặc thực hiện thủ công theo runbook.",
        )
        return
    if (
        mode == "single-full"
        and confirmed_full
        and _requires_destructive_confirmation(text)
        and not confirmed_destructive
    ):
        confirmation = _issue_destructive_confirmation(
            actor, chat_id, text, cluster_ref=current_cluster_ref,
        )
        await _send(
            bot_token,
            chat_id,
            "⚠️ XÁC NHẬN LỆNH NGUY HIỂM\n"
            f"Cụm: {cluster.name}\n"
            f"Yêu cầu có thể tác động service/dữ liệu:\n{text[:1200]}\n\n"
            f"Nếu vẫn chính xác, gửi: /confirm_destructive {confirmation}\n"
            f"Mã có hiệu lực {_DESTRUCTIVE_CONFIRM_TTL_SECONDS // 60} phút.",
        )
        return
    if mode == "single-full" and not confirmed_full:
        if not _sender_can_use_full_access(message):
            _set_mode(actor, "single")
            await _send(bot_token, chat_id, "Quyền Single Full đã bị thu hồi; đã quay về chế độ một AI.")
            return
        user_message = _save_message(
            session_id=session_id, cluster_id=cluster.id, actor=actor, role="user", content=text
        )
        del user_message
        confirmation = _issue_full_confirmation(
            actor, chat_id, text, cluster_ref=current_cluster_ref,
        )
        await _send(
            bot_token,
            chat_id,
            "⚠️ XÁC NHẬN SINGLE FULL\n"
            f"Cụm: {cluster.name}\n"
            f"Yêu cầu sẽ chạy với toàn quyền source và server:\n{text[:1200]}\n\n"
            f"Nếu chính xác, gửi: /confirm_full {confirmation}\n"
            f"Mã có hiệu lực {_FULL_CONFIRM_TTL_SECONDS // 60} phút.",
        )
        return

    if not confirmed_full:
        user_message = _save_message(
            session_id=session_id, cluster_id=cluster.id, actor=actor, role="user", content=text
        )
        del user_message

    # Match the web chatbox's explicit OK flow for a staged node command.
    # A confirmed Single Full request is a separate exact-task confirmation,
    # never an implicit confirmation of an earlier regular Chatbox proposal.
    pending = None
    if mode != "single-full":
        with db.SessionLocal() as session:
            pending = (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.session_id == session_id,
                    ChatMessage.actor == actor,
                    ChatMessage.cluster_id == cluster.id,
                    ChatMessage.role == "assistant",
                    ChatMessage.proposed_action_id == "execute_node_command",
                    ChatMessage.proposed_status == "PENDING",
                )
                .order_by(ChatMessage.created_at.desc())
                .first()
            )
    if pending is not None:
        if text != "OK":
            with db.SessionLocal() as session:
                row = session.get(ChatMessage, pending.id)
                if row is not None:
                    row.proposed_status = "CANCELLED"
                    session.commit()
            await _send(bot_token, chat_id, "Đề xuất lệnh trên node đã huỷ vì tin nhắn kế tiếp không phải chính xác OK.")
            return
        try:
            await _confirm_chat_action_core(pending.id, actor, allow_node_command=True)
            with db.SessionLocal() as session:
                row = session.get(ChatMessage, pending.id)
                action = session.query(Action).filter(Action.incident_id == row.proposed_incident_id).one()
                action_id = action.id
            approve_action_core(action_id, actor)
            await _send(bot_token, chat_id, "Đã xác nhận OK. Lệnh đã được chuyển cho Worker thực hiện.")
        except Exception as exc:
            logger.exception("telegram_chat: failed to confirm node command")
            await _send(bot_token, chat_id, f"Không thể xác nhận đề xuất: {exc}")
        return

    if mode == "single-full":
        if not _sender_can_use_full_access(message):
            _set_mode(actor, "single")
            await _send(bot_token, chat_id, "Quyền Single Full đã bị thu hồi; đã quay về chế độ một AI.")
            return
        run_id = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        with _full_runs_lock:
            already_running = any(
                state.get("chat_id") == chat_id and state.get("actor") == actor
                for state in _full_runs.values()
            )
            if not already_running:
                _full_runs[run_id] = {
                    "task": None,
                    "loop": loop,
                    "chat_id": chat_id,
                    "actor": actor,
                    "stop_requested": False,
                    "stage": "đang khởi chạy",
                    "started_at": time.monotonic(),
                }
        if already_running:
            await _send(
                bot_token, chat_id,
                "Single Full đang chạy cho yêu cầu trước; không khởi chạy trùng. Gửi /stop nếu cần dừng.",
            )
            return
        task = asyncio.create_task(
            _run_single_full_in_background(
                run_id=run_id, bot_token=bot_token, chat_id=chat_id, actor=actor,
                session_id=session_id, cluster_id=cluster.id, text=text, history=history,
                cluster_context=_single_full_cluster_context(actor, cluster),
            ),
            name=f"telegram-single-full-{run_id}",
        )
        def clear_cancelled_before_start(finished_task: asyncio.Task, *, pending_run_id: str = run_id) -> None:
            # A task can be cancelled before its coroutine begins, in which
            # case its own `finally` block cannot remove the pending state.
            if not finished_task.cancelled():
                return
            with _full_runs_lock:
                state = _full_runs.get(pending_run_id)
                if state is not None and state.get("task") is finished_task:
                    _full_runs.pop(pending_run_id, None)

        task.add_done_callback(clear_cancelled_before_start)
        with _full_runs_lock:
            state = _full_runs.get(run_id)
            if state is not None:
                state["task"] = task
                if state.get("stop_requested"):
                    task.cancel()
        await _send(
            bot_token, chat_id,
            "⚠️ Single Full đã khởi chạy với toàn quyền source và server. "
            "Bot vẫn nhận lệnh; gửi /stop để dừng phiên này.",
        )
        return
    if mode == "dual":
        run_id = uuid.uuid4().hex[:16]
        status_message_id: int | None = None
        run_state = {
            "task": asyncio.current_task(),
            "loop": asyncio.get_running_loop(),
            "token": bot_token,
            "chat_id": chat_id,
            "actor": actor,
            "status_message_id": None,
            "stop_requested": False,
            "stage": "đang trao đổi",
            "started_at": time.monotonic(),
        }
        with _dual_runs_lock:
            _dual_runs[run_id] = run_state
        try:
            ai_events = 0
            termination_reason = None
            await _send(bot_token, chat_id, f"👤 Câu hỏi Hai AI:\n{text}")
            status_message_id = await asyncio.to_thread(
                send_telegram_message_with_keyboard,
                bot_token,
                chat_id,
                "⏳ Hai AI đang trao đổi...\nBấm ⏹ Dừng để kết thúc phiên.",
                [("⏹ Dừng", f"{DUAL_STOP_PREFIX}{run_id}")],
            )
            with _dual_runs_lock:
                current = _dual_runs.get(run_id)
                if current is not None:
                    current["status_message_id"] = status_message_id
                    if current.get("stop_requested"):
                        await _edit_dual_status(
                            bot_token, chat_id, status_message_id,
                            "⏹ Đã dừng phiên Hai AI theo yêu cầu.",
                        )
                        return
            async for event in stream_dual_ai_chat(text, history, allow_writes=True):
                content = (
                    f"[Dual AI: {event.get('speaker', 'AI')} · {event.get('provider', '—')}]\n"
                    f"{event.get('content', '')}"
                )
                _save_message(
                    session_id=session_id, cluster_id=cluster.id, actor=actor,
                    role="assistant", content=content,
                )
                await _send(bot_token, chat_id, content)
                if event.get("termination_reason"):
                    termination_reason = event.get("termination_reason")
                elif event.get("speaker") != "Hệ thống":
                    ai_events += 1
            if termination_reason == "max_rounds":
                await _edit_dual_status(
                    bot_token, chat_id, status_message_id,
                    "⚠️ Hai AI đã dừng vì đạt giới hạn lượt trao đổi.",
                )
                await _send(
                    bot_token,
                    chat_id,
                    f"⚠️ ĐÃ DỪNG DO GIỚI HẠN\nHai AI đã đạt giới hạn {ai_events} lượt AI; chưa xác nhận là hoàn tất.",
                )
            else:
                await _edit_dual_status(
                    bot_token, chat_id, status_message_id,
                    "✅ Hai AI đã hoàn tất phiên trao đổi.",
                )
                await _send(
                    bot_token,
                    chat_id,
                    f"✅ HOÀN TẤT\nHai AI đã xử lý xong yêu cầu ({ai_events} lượt AI).",
                )
        except asyncio.CancelledError:
            await _edit_dual_status(
                bot_token, chat_id, status_message_id,
                "⏹ Đã dừng phiên Hai AI theo yêu cầu.",
            )
            return
        except DualAIChatExhausted as exc:
            alert = _quota_alert_text("Hai AI", exc)
            await _edit_dual_status(bot_token, chat_id, status_message_id, alert)
            await _send_quota_alert(bot_token, chat_id, "Hai AI", exc)
        except DualAIChatError as exc:
            await _edit_dual_status(
                bot_token, chat_id, status_message_id,
                f"⚠️ Phiên Hai AI đã dừng: {exc}",
            )
            await _send(bot_token, chat_id, f"Không thể tiếp tục chế độ hai AI: {exc}")
        except Exception:
            logger.exception("telegram_chat: dual mode failed")
            await _edit_dual_status(
                bot_token, chat_id, status_message_id,
                "❌ Phiên Hai AI gặp lỗi; kiểm tra log Dashboard.",
            )
            await _send(bot_token, chat_id, "Chế độ hai AI gặp lỗi nội bộ; kiểm tra log Dashboard.")
        finally:
            with _dual_runs_lock:
                _dual_runs.pop(run_id, None)
        return

    try:
        result = await run_chat_turn(history, text, _model_actor(), cluster)
        proposal = result.get("proposal")
        reply = _proposal_text(result.get("reply_text") or "(không có phản hồi)", proposal)
        assistant = _save_message(
            session_id=session_id, cluster_id=cluster.id, actor=actor,
            role="assistant", content=reply, proposal=proposal,
            proposed_status="PENDING" if proposal else None,
        )
        if proposal and proposal.get("action_id") != "execute_node_command":
            await _send_proposal(bot_token, chat_id, reply, assistant)
        else:
            if proposal and proposal.get("action_id") == "execute_node_command":
                reply += "\n\nNhập chính xác OK ở tin nhắn kế tiếp để thực hiện."
            await _send(bot_token, chat_id, reply)
    except ChatTurnError as exc:
        logger.warning("telegram_chat: single mode failed: %s", exc)
        await _send(bot_token, chat_id, f"Không thể trả lời: {exc}")
    except Exception:
        logger.exception("telegram_chat: unexpected single mode failure")
        try:
            await _send(bot_token, chat_id, "Không thể trả lời do lỗi nội bộ; kiểm tra log Dashboard.")
        except Exception:
            logger.exception("telegram_chat: failed to report single mode failure")


async def handle_callback(callback_query: dict, bot_token: str) -> str | None:
    """Confirm a Chatbox proposal from its inline Telegram button."""
    if not is_allowed_callback(callback_query, bot_token):
        return None
    data = str(callback_query.get("data") or "")
    chat_id = str(((callback_query.get("message") or {}).get("chat") or {}).get("id", ""))
    actor = _actor(callback_query)
    if data.startswith(AI_MODE_PREFIX):
        selected = data[len(AI_MODE_PREFIX):]
        label = await _select_mode(
            bot_token, chat_id, actor, selected,
            full_access_allowed=_sender_can_use_full_access(callback_query),
        )
        return f"Đã chọn {label}." if label else "Không thể chọn chế độ này."
    if data.startswith(CLUSTER_SELECT_PREFIX):
        selected_id = data[len(CLUSTER_SELECT_PREFIX):].strip()
        try:
            clusters = await asyncio.wait_for(
                asyncio.to_thread(_active_clusters),
                timeout=_CLUSTER_LOOKUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("telegram_chat: cluster selection validation timed out")
            return "DB cụm đang phản hồi chậm; hãy thử lại sau vài giây."
        except Exception:
            logger.exception("telegram_chat: failed to validate Telegram cluster selection")
            return "Không tải được danh sách cụm Ceph."
        selected = next((item for item in clusters if item["id"] == selected_id), None)
        if selected is None:
            return "Cụm không tồn tại hoặc đã tắt."
        _set_cluster(actor, selected_id)
        message = callback_query.get("message") or {}
        original = str(message.get("text") or "🔌 Chọn cụm Ceph")
        try:
            await asyncio.to_thread(
                edit_telegram_message,
                bot_token, chat_id, message.get("message_id"),
                f"{original}\n\n✅ Đã chọn cụm: {selected['name']}",
            )
        except Exception:
            logger.exception("telegram_chat: failed to close cluster selector message")
        return f"Đã chọn cụm {selected['name']}."
    if data == f"{QUOTA_LOGIN_PREFIX}codex":
        if not _sender_can_use_full_access(callback_query):
            return "Bạn không có quyền đổi tài khoản Codex của Single Full."
        owner_chat_id = str((callback_query.get("from") or {}).get("id", "")).strip()
        if not owner_chat_id.isdigit():
            return "Không xác định được Telegram owner để gửi đăng nhập riêng."
        try:
            login = await start_cli_device_login()
            verification_url = str(login.get("verificationUrl") or "").strip()
            user_code = str(login.get("userCode") or "").strip()
            if not verification_url or not user_code:
                raise CodexAppServerError("Codex CLI không trả về liên kết hoặc mã đăng nhập")
        except CodexAppServerError as exc:
            logger.warning("telegram_chat: could not start Codex device login: %s", exc)
            return f"Không thể bắt đầu đăng nhập Codex: {exc}"
        except Exception:
            logger.exception("telegram_chat: unexpected Codex device-login failure")
            return "Không thể bắt đầu đăng nhập Codex; kiểm tra log Dashboard."
        try:
            # Device codes can authorize replacing the server's default Codex
            # account. Never disclose them to the configured group: Telegram
            # permits a bot to DM only a user who has opened its private chat.
            await _send(
                bot_token,
                owner_chat_id,
                "🔑 ĐĂNG NHẬP CODEX TÀI KHOẢN KHÁC\n"
                "Mở liên kết dưới đây, đăng nhập bằng tài khoản bạn muốn đổi sang rồi nhập mã:\n"
                f"{verification_url}\n\n"
                f"Mã: {user_code}\n\n"
                "Sau khi xác thực xong, tài khoản Codex mặc định của Single Full sẽ được thay thế. "
                "Gửi một yêu cầu AI mới để chạy bằng tài khoản đó.",
            )
        except Exception:
            logger.exception("telegram_chat: could not DM Codex device-login details")
            return "Hãy mở chat riêng với bot, bấm Start một lần rồi bấm lại nút đăng nhập."
        login_id = str(login.get("loginId") or "").strip()
        if login_id:
            _watch_codex_login(login_id=login_id, bot_token=bot_token, chat_id=owner_chat_id)
        return "Đã gửi riêng liên kết và mã đăng nhập Codex."
    if data.startswith(DUAL_STOP_PREFIX):
        run_id = data[len(DUAL_STOP_PREFIX):]
        with _dual_runs_lock:
            run_state = _dual_runs.get(run_id)
            if run_state is not None and run_state.get("actor") != actor:
                return "Bạn không phải người bắt đầu phiên Hai AI này."
            if run_state is not None:
                run_state["stop_requested"] = True
                task = run_state.get("task")
                loop = run_state.get("loop")
                status_message_id = run_state.get("status_message_id")
                if status_message_id is not None and task is not None and loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(task.cancel)
            else:
                task = loop = status_message_id = None
        if run_state is None:
            return "Phiên Hai AI đã kết thúc."
        await _edit_dual_status(
            bot_token,
            _chat_id(callback_query.get("message") or {}),
            status_message_id,
            "⏹ Đã nhận yêu cầu dừng phiên Hai AI...",
        )
        return "Đã yêu cầu dừng phiên Hai AI."
    if data.startswith(CHAT_APPROVE_PREFIX):
        message_id = telegram_federation.unqualify_reference(data[len(CHAT_APPROVE_PREFIX):])
        with db.SessionLocal() as session:
            row = session.get(ChatMessage, message_id)
            if row is None or row.actor != actor or not row.proposed_incident_id:
                return "Không tìm thấy yêu cầu cần duyệt."
            action = session.query(Action).filter(Action.incident_id == row.proposed_incident_id).first()
            if action is None:
                return "Yêu cầu không còn tồn tại."
            action_id = action.id
        result = approve_action_core(action_id, actor)
        message = callback_query.get("message") or {}
        original = str(message.get("text") or "")
        suffix = (
            "✅ ĐÃ DUYỆT; Worker sẽ thực hiện."
            if result.outcome == ApprovalOutcome.APPROVED
            else "ℹ️ Action đã được xử lý trước đó."
        )
        await asyncio.to_thread(
            edit_telegram_message, bot_token, chat_id, message.get("message_id"), original + "\n\n" + suffix
        )
        return suffix
    if not data.startswith(CHAT_CONFIRM_PREFIX):
        return None
    message_id = telegram_federation.unqualify_reference(data[len(CHAT_CONFIRM_PREFIX):])
    try:
        await _confirm_chat_action_core(message_id, actor)
        with db.SessionLocal() as session:
            row = session.get(ChatMessage, message_id)
            detail = "Đã tạo yêu cầu; Worker sẽ xử lý theo chính sách an toàn."
            needs_final_approval = False
            if row is not None and row.proposed_incident_id:
                action = session.query(Action).filter(Action.incident_id == row.proposed_incident_id).first()
                if action is not None and action.status == ActionStatus.APPROVED.value:
                    detail = "Đã xác nhận; Worker đã nhận action an toàn."
                elif action is not None and action.status == ActionStatus.PENDING_APPROVAL.value:
                    needs_final_approval = True
        message = callback_query.get("message") or {}
        original = str(message.get("text") or "")
        suffix = "✅ ĐÃ XÁC NHẬN." if not needs_final_approval else "✅ ĐÃ TẠO YÊU CẦU — cần duyệt cuối."
        await asyncio.to_thread(
            edit_telegram_message, bot_token, chat_id, message.get("message_id"), original + "\n\n" + suffix
        )
        if needs_final_approval:
            await asyncio.to_thread(
                send_telegram_message_with_keyboard,
                bot_token,
                chat_id,
                "⚠️ Action RISKY/DESTRUCTIVE cần một lần duyệt cuối trước khi Worker thực hiện.",
                [
                    (
                        "✅ Duyệt cuối",
                        f"{CHAT_APPROVE_PREFIX}{telegram_federation.qualify_reference(message_id)}",
                    )
                ],
            )
            detail = "Đề xuất đã tạo. Hãy bấm “Duyệt cuối” để cho phép Worker thực hiện."
        return detail
    except Exception as exc:
        logger.exception("telegram_chat: proposal confirmation failed")
        return f"Không thể xác nhận đề xuất: {exc}"


async def handle_message(message: dict, bot_token: str) -> None:
    """Handle one message using the database belonging to its cluster."""
    if not is_allowed_message(message, bot_token):
        return
    actor = _actor(message)
    text = str(message.get("text") or "").strip().lower()
    command_name = text.split(maxsplit=1)[0].split("@", 1)[0] if text.startswith("/") else ""
    command_only = {
        "/help", "/start", "/status", "/cluster", "/clusters", "/single", "/dual",
        "/single_full", "/single-full", "/model", "/mode", "/stop", "/new",
    }
    if command_name in command_only:
        await _handle_message_impl(message, bot_token, cluster_override=None)
        return
    resolved = await asyncio.to_thread(_resolve_cluster_for_actor, actor)
    if resolved is None:
        await _handle_message_impl(message, bot_token, cluster_override=None)
        return
    target, cluster = resolved
    with db.use_database(target.source.url):
        await _handle_message_impl(message, bot_token, cluster_override=cluster)


def run_message_sync(message: dict, bot_token: str) -> None:
    with _dashboard_loop_lock:
        loop = _dashboard_loop
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(handle_message(message, bot_token), loop).result()
        return
    asyncio.run(handle_message(message, bot_token))


def run_callback_sync(callback_query: dict, bot_token: str) -> str | None:
    # Device-auth callbacks own an async Codex CLI process until the operator
    # finishes browser login.  Keep them on Dashboard's long-lived loop;
    # asyncio.run() would tear down the drain task immediately on return.
    data = str(callback_query.get("data") or "")
    database_url = None
    if data.startswith((CHAT_CONFIRM_PREFIX, CHAT_APPROVE_PREFIX)):
        message_ref = data.split(":", 1)[1].strip()
        database_urls = telegram_federation.database_urls_for_message_reference(message_ref)
        if len(database_urls) > 1:
            return "Yêu cầu cũ bị trùng giữa các DB; hãy gửi lại yêu cầu sau khi chọn đúng cụm."
        database_url = database_urls[0] if database_urls else None

    async def callback_in_database() -> str | None:
        with db.use_database(database_url):
            return await handle_callback(callback_query, bot_token)

    with _dashboard_loop_lock:
        loop = _dashboard_loop
    if loop is not None and loop.is_running():
        return asyncio.run_coroutine_threadsafe(callback_in_database(), loop).result()
    return asyncio.run(callback_in_database())


def _message_worker() -> None:
    while True:
        message, bot_token = _message_queue.get()
        try:
            run_message_sync(message, bot_token)
        except Exception:
            logger.exception("telegram_chat: queued message failed")
        finally:
            _message_queue.task_done()


def enqueue_message(message: dict, bot_token: str) -> bool:
    """Queue an authorized message without blocking Telegram polling.

    The single daemon worker preserves message ordering for the configured
    chat while allowing approval callbacks and the long-poll loop to continue
    during a long single/dual AI turn.
    """
    if not is_allowed_message(message, bot_token):
        return False
    global _message_worker_started
    with _message_worker_lock:
        if not _message_worker_started:
            thread = threading.Thread(target=_message_worker, name="telegram-chat-worker", daemon=True)
            thread.start()
            _message_worker_started = True
    try:
        _message_queue.put_nowait((message, bot_token))
    except queue.Full:
        try:
            send_telegram_message(
                bot_token,
                str((message.get("chat") or {}).get("id", "")),
                "Chatbox đang xử lý các yêu cầu trước đó; vui lòng thử lại sau.",
            )
        except Exception:
            logger.exception("telegram_chat: failed to report full queue")
        return False
    return True
