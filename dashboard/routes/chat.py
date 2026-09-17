import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, text as sql_text

from config.settings import settings
from dashboard.chat_client import (
    ChatTurnError,
    MAX_HISTORY_MESSAGES,
    MISSING_AI_CONFIG_MESSAGE,
    run_chat_turn,
    with_romantic_address,
)
from dashboard.dual_ai_chat import (
    MAX_DUAL_PROMPT_CHARS,
    DualAIChatError,
    DualAIChatExhausted,
    stream_dual_ai_chat,
)
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.cluster_scope import selected_cluster
from dashboard.vntime import to_utc_iso
from shared import audit, db
from shared.ai_limits import normalize_rate_limits
from shared.claude_cli import ClaudeCLIError, claude_status
from shared.codex_app_server import CodexAppServerError, codex_app_server
from shared.cluster_nodes import configured_nodes
from shared.ai_delegation import enqueue_task
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    ChatMessage,
    ChatPreference,
    Incident,
    IncidentStatus,
    Cluster,
    DelegatedAITask,
    DelegatedAISubtask,
)
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.llm.router_client import VALID_ACTION_IDS
from worker.policy import gate
from worker.policy.gate import VALID_BLUESTORE_ACTION_IDS, VALID_MANAGEMENT_ACTION_IDS

logger = logging.getLogger(__name__)

_DELEGATED_ACTIVE_STATUSES = ("QUEUED", "PLANNING", "RUNNING", "AGGREGATING")

router = APIRouter()

# How many past messages the widget fetches on page load — purely a display
# limit (this is cheap, unlike MAX_HISTORY_MESSAGES which bounds what's
# actually replayed into Claude's token context on every turn).
CHAT_WIDGET_HISTORY_LIMIT = 200

# How many of the most recent messages (across ALL sessions) get scanned to
# build the session list (GET /api/chat/sessions) — bounds that query's cost
# regardless of how large this table eventually grows. A session whose every
# message falls outside this window just won't appear in the list — its
# data is untouched, only not listed — an acceptable trade-off for a
# single-operator tool where this many messages represents a lot of real use.
SESSION_LIST_SCAN_LIMIT = 2000
# How much of a session's opening question is shown as its list "title" —
# same truncate-for-display posture as DIAGNOSTIC_OUTPUT_MAX_CHARS
# elsewhere in this codebase, just much shorter (a one-line preview).
SESSION_PREVIEW_MAX_CHARS = 80

# ceph_code used for the synthetic Incident created when an operator
# confirms a chat-proposed action — never emitted by the real Watcher (real
# codes come from `ceph health detail`'s check names), so this value alone
# is enough to tell a chat-originated Incident apart from a detected one if
# ever needed, on top of the EVENT_CHAT_ACTION_REQUESTED audit entry.
CHAT_REQUEST_CEPH_CODE = "CHAT_REQUEST"
MAX_AI_NAME_LENGTH = 64
MAX_FEMALE_ADDRESS_LENGTH = 128
MAX_ACTIVE_DUAL_JOBS = 2
_DUAL_JOB_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_DUAL_JOBS)
_DUAL_JOBS: dict[tuple[str, int, str], asyncio.Task] = {}


def _dual_job_key(actor: str, cluster_id: int, session_id: str) -> tuple[str, int, str]:
    return actor, cluster_id, session_id


def _forget_dual_job(key: tuple[str, int, str], task: asyncio.Task) -> None:
    if _DUAL_JOBS.get(key) is task:
        _DUAL_JOBS.pop(key, None)


def _validated_ai_name(value) -> str:
    name = str(value or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Tên AI không được để trống")
    if len(name) > MAX_AI_NAME_LENGTH:
        raise HTTPException(status_code=400, detail=f"Tên AI tối đa {MAX_AI_NAME_LENGTH} ký tự")
    if not all(ch.isalnum() or ch in " -_." for ch in name):
        raise HTTPException(
            status_code=400,
            detail="Tên AI chỉ được chứa chữ, số, khoảng trắng, dấu gạch, dấu chấm hoặc gạch dưới",
        )
    return name


def _validated_female_address(value) -> str:
    address = str(value or "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="Cách xưng hô nữ không được để trống")
    if len(address) > MAX_FEMALE_ADDRESS_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Cách xưng hô nữ tối đa {MAX_FEMALE_ADDRESS_LENGTH} ký tự",
        )
    if any(ch in address for ch in "\r\n\x00"):
        raise HTTPException(status_code=400, detail="Cách xưng hô nữ chỉ được nằm trên một dòng")
    return address


def _lock_delegated_admission(session) -> None:
    """Serialize admission checks with task creation for every supported DB."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        # SQLite has no row/advisory lock. BEGIN IMMEDIATE takes the database
        # write lock before the count, so another process cannot pass the
        # check and insert a task concurrently.
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    elif dialect == "postgresql":
        session.execute(
            sql_text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": "ceph-ai:delegated-admission"},
        )
    else:
        raise RuntimeError(f"Unsupported database for delegated admission lock: {dialect}")


def _check_delegated_admission(session, actor: str) -> None:
    """Bound delegated backlog atomically with the task insert transaction."""
    _lock_delegated_admission(session)
    now = datetime.utcnow()
    global_active = (
        session.query(DelegatedAITask)
        .filter(DelegatedAITask.status.in_(_DELEGATED_ACTIVE_STATUSES))
        .count()
    )
    if global_active >= settings.delegated_ai_max_active_tasks:
        raise HTTPException(
            status_code=429,
            detail="Đang có quá nhiều delegated task hoạt động; vui lòng thử lại sau.",
        )
    actor_active = (
        session.query(DelegatedAITask)
        .filter(
            DelegatedAITask.actor == actor,
            DelegatedAITask.status.in_(_DELEGATED_ACTIVE_STATUSES),
        )
        .count()
    )
    if actor_active >= settings.delegated_ai_max_active_tasks_per_actor:
        raise HTTPException(
            status_code=429,
            detail="Bạn đã có delegated task đang chạy; chờ task hiện tại hoàn tất.",
        )
    latest = (
        session.query(DelegatedAITask)
        .filter(DelegatedAITask.actor == actor)
        .order_by(DelegatedAITask.created_at.desc())
        .first()
    )
    cooldown = settings.delegated_ai_submit_cooldown_seconds
    if latest is not None and cooldown > 0 and latest.created_at > now - timedelta(seconds=cooldown):
        raise HTTPException(
            status_code=429,
            detail=f"Vui lòng chờ {cooldown} giây giữa hai delegated task.",
        )


@router.get("/api/chat/preferences")
async def get_chat_preferences(user: str = Depends(require_login)):
    return {
        "ai_name": auth.chat_ai_name(user),
        "female_address": auth.chat_female_address(user),
    }


@router.get("/api/chat/limits")
async def get_chat_ai_limits(user: str = Depends(require_login)):
    """Current subscription quota for the provider serving this chatbox."""
    codex_error = None
    if settings.codex_chat_enabled:
        try:
            return {"provider": "codex", "limits": normalize_rate_limits(await codex_app_server.rate_limits())}
        except CodexAppServerError as exc:
            # Keep the quota widget aligned with the chat call order: a
            # broken Codex session should display the configured Claude
            # fallback instead of making the UI look like no AI is usable.
            codex_error = str(exc)
        else:
            codex_error = None
    if settings.claude_chat_enabled:
        try:
            status = await claude_status()
            payload = {"provider": "claude", "limits": normalize_rate_limits(status.get("rate_limits"))}
            if codex_error:
                payload["fallback_from"] = "codex"
            return payload
        except ClaudeCLIError as exc:
            return {"provider": "claude", "limits": [], "error": str(exc),
                    **({"codex_error": codex_error} if codex_error else {})}
    if settings.codex_chat_enabled:
        return {"provider": "codex", "limits": [], "error": codex_error}
    return {"provider": None, "limits": []}


@router.put("/api/chat/preferences")
async def update_chat_preferences(request: Request, user: str = Depends(require_login)):
    body = await request.json()
    ai_name = _validated_ai_name(body.get("ai_name"))
    female_address = _validated_female_address(body.get("female_address"))
    with db.SessionLocal() as session:
        preference = session.get(ChatPreference, user)
        if preference is None:
            preference = ChatPreference(
                username=user, ai_name=ai_name, female_address=female_address
            )
            session.add(preference)
        else:
            preference.ai_name = ai_name
            preference.female_address = female_address
        session.commit()
    return {"ai_name": ai_name, "female_address": female_address}


def _message_to_dict(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "session_id": message.session_id,
        "cluster_id": message.cluster_id,
        "role": message.role,
        "content": message.content,
        "actor": message.actor,
        "proposed_action_id": message.proposed_action_id,
        "proposed_target_nodes": (
            json.loads(message.proposed_target_nodes) if message.proposed_target_nodes else None
        ),
        "proposed_action_params": (
            json.loads(message.proposed_action_params) if message.proposed_action_params else None
        ),
        "proposed_rationale": message.proposed_rationale,
        "proposed_command_preview": message.proposed_command_preview,
        "proposed_status": message.proposed_status,
        "tools_used": json.loads(message.tools_used) if message.tools_used else None,
        "created_at": to_utc_iso(message.created_at),
    }


def _dual_event_content(event: dict) -> str:
    return (
        f"[Dual AI: {event.get('speaker', 'AI')} · {event.get('provider', '—')}]\n"
        f"{event.get('content', '')}"
    )


def _persist_dual_event(event: dict, *, session_id: str, cluster_id: int, actor: str) -> None:
    """Persist one dual-AI event so the widget's existing poller can display it."""
    with db.SessionLocal() as session:
        message = ChatMessage(
            session_id=session_id,
            cluster_id=cluster_id,
            role="assistant",
            content=_dual_event_content(event),
            actor=actor,
        )
        session.add(message)
        session.commit()


async def _run_dual_chat_background(
    prompt: str,
    history: list[dict],
    session_id: str,
    cluster_id: int,
    actor: str,
) -> None:
    """Run the exchange after the HTTP response and persist every turn."""
    # This is a non-blocking check; calling the threading semaphore directly
    # avoids leaving a background acquire in a worker thread if the task is
    # cancelled by the Stop button at exactly this point.
    acquired = _DUAL_JOB_SLOTS.acquire(blocking=False)
    if not acquired:
        try:
            await asyncio.to_thread(
                _persist_dual_event,
                {
                    "speaker": "Hệ thống",
                    "provider": "—",
                    "content": "Đang có quá nhiều phiên Hai AI chạy đồng thời; vui lòng thử lại sau.",
                },
                session_id=session_id,
                cluster_id=cluster_id,
                actor=actor,
            )
        except Exception:
            logger.exception("dual AI background job could not persist capacity error")
        return
    try:
        async for event in stream_dual_ai_chat(prompt, history):
            await asyncio.to_thread(
                _persist_dual_event,
                event,
                session_id=session_id,
                cluster_id=cluster_id,
                actor=actor,
            )
    except DualAIChatExhausted as exc:
        logger.info("dual AI background job stopped at provider token/quota limit: %s", exc)
        try:
            await asyncio.to_thread(
                _persist_dual_event,
                {
                    "speaker": "Hệ thống",
                    "provider": "—",
                    "content": "Đã dừng trao đổi: provider hết token hoặc quota.",
                },
                session_id=session_id,
                cluster_id=cluster_id,
                actor=actor,
            )
        except Exception:
            logger.exception("dual AI background job could not persist token-limit status")
    except asyncio.CancelledError:
        logger.info("dual AI background job stopped by operator")
        try:
            await asyncio.to_thread(
                _persist_dual_event,
                {
                    "speaker": "Hệ thống",
                    "provider": "—",
                    "content": "Đã dừng trao đổi theo yêu cầu.",
                },
                session_id=session_id,
                cluster_id=cluster_id,
                actor=actor,
            )
        except Exception:
            logger.exception("dual AI background job could not persist cancellation status")
        raise
    except DualAIChatError as exc:
        logger.warning("dual AI background job failed: %s", exc)
        error_event = {
            "speaker": "Hệ thống",
            "provider": "—",
            "content": f"Không thể tiếp tục chế độ hai AI: {exc}",
        }
        try:
            await asyncio.to_thread(
                _persist_dual_event,
                error_event,
                session_id=session_id,
                cluster_id=cluster_id,
                actor=actor,
            )
        except Exception:
            logger.exception("dual AI background job could not persist its error")
    except Exception:
        logger.exception("dual AI background job crashed")
        try:
            await asyncio.to_thread(
                _persist_dual_event,
                {
                    "speaker": "Hệ thống",
                    "provider": "—",
                    "content": "Chế độ hai AI gặp lỗi nội bộ; kiểm tra log Dashboard.",
                },
                session_id=session_id,
                cluster_id=cluster_id,
                actor=actor,
            )
        except Exception:
            logger.exception("dual AI background job could not persist internal error")
    finally:
        _DUAL_JOB_SLOTS.release()


_NO_MESSAGES_YET = object()  # sentinel — distinct from "the latest row's session_id happens to be None"


def _message_cluster_filter(cluster):
    condition = ChatMessage.cluster_id == cluster.id
    return or_(condition, ChatMessage.cluster_id.is_(None)) if cluster.is_default else condition


def _latest_session_id(session, actor: str, cluster=None):
    """"The current session" is the latest session owned by ``actor``.
    There is no separate session table to go out of sync with the messages.

    Returns `_NO_MESSAGES_YET` when the table is empty (fresh install, or
    right after a brand new session id was handed out but nothing's been
    posted to it yet) — deliberately NOT plain `None` for that case, because
    a row's `session_id` can itself legitimately be `None` (pre-migration
    legacy data in a table that predates this column, or a test fixture
    that didn't set one) and callers must be able to tell "no history at
    all" apart from "history exists, tagged with a null session_id" —
    conflating the two here made get_chat_messages() incorrectly report an
    empty conversation for the latter case.
    """
    query = session.query(ChatMessage).filter(ChatMessage.actor == actor)
    if cluster is not None:
        query = query.filter(_message_cluster_filter(cluster))
    latest = (
        query
        .order_by(ChatMessage.created_at.desc())
        .first()
    )
    return _NO_MESSAGES_YET if latest is None else latest.session_id


@router.post("/api/chat/sessions")
async def create_chat_session(user: str = Depends(require_login)):
    """"Tạo đoạn chat mới" — hands the frontend a fresh session id to tag its
    next message with. Writes nothing by itself (see ChatMessage's
    docstring): a session only starts existing once a message actually uses
    this id, which is also why get_chat_messages() below will keep returning
    the PREVIOUS session's history until that first message is sent — there
    is nothing to show for an empty new session anyway (the frontend clears
    its view to the empty state immediately, optimistically, on this call's
    response)."""
    return {"session_id": str(uuid.uuid4())}


def _build_session_summaries(session, actor: str, cluster=None) -> list[dict]:
    """One summary per session owned by ``actor``, newest-active first —
    powers the chat panel's history list. Scans only the most recent
    SESSION_LIST_SCAN_LIMIT messages (bounds cost regardless of how large
    this table eventually grows).

    `rows` comes back newest-first; walking it in that order means the
    FIRST row seen for a given session_id is its most recent message (->
    last_active_at), while `preview` keeps getting overwritten on every
    user-role row seen — so by the time the loop reaches that session's
    oldest row, `preview` has settled on the OLDEST (opening) user message,
    a natural one-line title for the conversation.
    """
    query = session.query(ChatMessage).filter(ChatMessage.actor == actor)
    if cluster is not None:
        query = query.filter(_message_cluster_filter(cluster))
    rows = (
        query
        .order_by(ChatMessage.created_at.desc())
        .limit(SESSION_LIST_SCAN_LIMIT)
        .all()
    )
    by_session: dict = {}
    for m in rows:
        entry = by_session.get(m.session_id)
        if entry is None:
            entry = {
                "session_id": m.session_id,
                "message_count": 0,
                "last_active_at": m.created_at,
                "started_at": m.created_at,
                "preview": None,
            }
            by_session[m.session_id] = entry
        entry["message_count"] += 1
        entry["started_at"] = m.created_at  # newest-first scan -> last write wins -> ends up oldest
        if m.role == "user":
            entry["preview"] = m.content[:SESSION_PREVIEW_MAX_CHARS]

    summaries = sorted(by_session.values(), key=lambda e: e["last_active_at"], reverse=True)
    for entry in summaries:
        entry["last_active_at"] = to_utc_iso(entry["last_active_at"])
        entry["started_at"] = to_utc_iso(entry["started_at"])
        if entry["preview"] is None:
            entry["preview"] = "(không có nội dung)"
    return summaries


@router.get("/api/chat/sessions")
async def list_chat_sessions(request: Request, user: str = Depends(require_login)):
    """Powers the per-login chat history — one row per past conversation,
    most-recently-active first, so the operator can browse or delete an old
    session without it ever having to be "the current one" again."""
    with db.SessionLocal() as session:
        cluster = selected_cluster(request)
        summaries = _build_session_summaries(session, user, cluster)
        current = _latest_session_id(session, user, cluster)
        for entry in summaries:
            entry["is_current"] = entry["session_id"] == current
        return {"sessions": summaries}


@router.get("/api/chat/sessions/{session_id}")
async def get_chat_session(session_id: str, request: Request, user: str = Depends(require_login)):
    """Return one saved conversation for the history viewer.

    The lookup is scoped to the logged-in actor and selected cluster so a
    history id cannot be used to read another operator's or cluster's chat.
    """
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        rows = (
            session.query(ChatMessage)
            .filter(
                ChatMessage.session_id == session_id,
                ChatMessage.actor == user,
                _message_cluster_filter(cluster),
            )
            .order_by(ChatMessage.created_at.asc())
            .limit(CHAT_WIDGET_HISTORY_LIMIT)
            .all()
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Không tìm thấy đoạn chat")
        return {
            "session_id": session_id,
            "messages": [_message_to_dict(message) for message in rows],
        }


@router.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str, request: Request, user: str = Depends(require_login)):
    """Permanently deletes every message in one session — the only
    destructive endpoint across Story 6.1-6.3 (everything else is additive
    or reversible-in-spirit, e.g. starting a new session doesn't delete the
    old one). Does NOT touch any Incident/Action a confirmed proposal in
    this session may have created — those are independent, real
    infrastructure state (Story 6.1's confirm-action already copied
    everything the Worker/approval pipeline needs into their own rows), and
    nothing in this schema has a foreign key pointing AT ChatMessage.id
    except the delegated-task assistant-message link. Active delegated tasks
    are first marked CANCELLED so the Worker can observe the cancellation;
    delegated task and subtask rows are then removed before the messages.
    Otherwise PostgreSQL correctly rejects the message delete with a
    ForeignKeyViolation and the UI reports HTTP 500. Confirmed Incidents and
    Actions remain untouched.
    """
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        message_filter = (
            ChatMessage.session_id == session_id,
            ChatMessage.actor == user,
            _message_cluster_filter(cluster),
        )
        message_ids = [
            row.id for row in session.query(ChatMessage.id).filter(*message_filter).all()
        ]
        if not message_ids:
            raise HTTPException(status_code=404, detail="Không tìm thấy đoạn chat")

        # DelegatedAITask.assistant_message_id is a non-nullable FK to
        # ChatMessage. Delete children explicitly for SQLite compatibility;
        # PostgreSQL would cascade subtasks through task_id, but relying on
        # database-specific FK settings would make this endpoint fragile in
        # tests and operator recovery databases.
        task_filter = (
            DelegatedAITask.session_id == session_id,
            DelegatedAITask.actor == user,
            DelegatedAITask.cluster_id == cluster.id,
        )
        task_rows = session.query(
            DelegatedAITask.id, DelegatedAITask.status
        ).filter(*task_filter).all()
        task_ids = [row.id for row in task_rows]
        active_task_ids = [
            row.id for row in task_rows if row.status in _DELEGATED_ACTIVE_STATUSES
        ]
        if task_ids:
            if active_task_ids:
                # Publish cancellation before removing the rows. The Worker
                # watches this status and can stop an in-flight provider call;
                # deleting the task in the same transaction would hide the
                # cancellation signal from that watcher.
                now = datetime.utcnow()
                session.query(DelegatedAISubtask).filter(
                    DelegatedAISubtask.task_id.in_(active_task_ids),
                    DelegatedAISubtask.status.not_in(("COMPLETED", "FAILED", "CANCELLED")),
                ).update(
                    {
                        "status": "CANCELLED",
                        "error": "Sub-agent bị hủy vì operator xoá lịch sử chat",
                        "lease_until": None,
                        "finished_at": now,
                        "updated_at": now,
                    },
                    synchronize_session=False,
                )
                session.query(DelegatedAITask).filter(
                    DelegatedAITask.id.in_(active_task_ids),
                    DelegatedAITask.status.in_(_DELEGATED_ACTIVE_STATUSES),
                ).update(
                    {
                        "status": "CANCELLED",
                        "error": "Đã hủy vì operator xoá lịch sử chat",
                        "lease_until": None,
                        "finished_at": now,
                        "updated_at": now,
                    },
                    synchronize_session=False,
                )
                # This is intentionally a small first transaction: the
                # Worker must be able to observe CANCELLED before the FK
                # rows are removed in the cleanup transaction below.
                session.commit()

            session.query(DelegatedAISubtask).filter(
                DelegatedAISubtask.task_id.in_(task_ids)
            ).delete(synchronize_session=False)
            session.query(DelegatedAITask).filter(
                DelegatedAITask.id.in_(task_ids)
            ).delete(synchronize_session=False)

        deleted = session.query(ChatMessage).filter(*message_filter).delete(
            synchronize_session=False
        )
        session.commit()
    return {"deleted": deleted}


@router.get("/api/chat/messages")
async def get_chat_messages(request: Request, user: str = Depends(require_login)):
    """Backs the floating chat widget (dashboard/static/chat_widget.js) on
    page load — the widget has no server-rendered history of its own (it's
    embedded directly in dashboard/templates/index.html, not its own route/
    template), so it fetches the recent transcript over this endpoint
    instead. Scoped to the CURRENT session only (_latest_session_id) — older
    sessions still exist in the DB, just not shown once a newer one has a
    message in it."""
    with db.SessionLocal() as session:
        cluster = selected_cluster(request)
        session_id = _latest_session_id(session, user, cluster)
        if session_id is _NO_MESSAGES_YET:
            return {"messages": [], "session_id": None}
        rows = (
            session.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id, ChatMessage.actor == user, _message_cluster_filter(cluster))
            .order_by(ChatMessage.created_at.asc())
            .limit(CHAT_WIDGET_HISTORY_LIMIT)
            .all()
        )
        return {"messages": [_message_to_dict(m) for m in rows], "session_id": session_id}


@router.post("/api/chat/messages")
async def post_chat_message(
    request: Request,
    user: str = Depends(require_login),
):
    cluster = selected_cluster(request)
    body = await request.json()
    text = (body.get("content") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Nội dung tin nhắn không được để trống")
    mode = (body.get("mode") or "single").strip().lower()
    dashboard_context = body.get("dashboard_context")
    ai_text = text
    if mode == "single" and isinstance(dashboard_context, dict):
        # The browser snapshot is only a helpful, bounded hint. It is not
        # trusted as an operational fact; the model must still call Ceph tools
        # for authoritative answers.
        allowed_context_keys = {
            "cluster", "health", "osds", "mons", "utilization",
            "placement_groups", "stale", "age_seconds",
        }
        context = {
            key: dashboard_context[key]
            for key in allowed_context_keys
            if key in dashboard_context
        }
        context_text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(context_text) <= 2400:
            ai_text = (
                "[Context snapshot từ Dashboard hiện tại — có thể đã cũ; "
                "hãy dùng tool Ceph để kiểm chứng nếu cần]\n"
                + context_text
                + "\n\nCâu hỏi của operator: "
                + text
            )
    if mode == "delegate":
        if len(text) > settings.delegated_ai_max_prompt_chars:
            raise HTTPException(
                status_code=413,
                detail=f"Delegated prompt tối đa {settings.delegated_ai_max_prompt_chars} ký tự.",
            )
    if mode not in {"single", "delegate"}:
        raise HTTPException(
            status_code=400,
            detail="Chế độ Hai AI chỉ khả dụng qua Telegram Chatbox.",
        )
    # Falls back to a fresh id rather than 400ing — a stale/cached frontend
    # bundle that never learned about sessions at all should still work,
    # same "degrade gracefully" posture as open_mcp_tools() elsewhere in
    # this feature. A blank/missing session_id here always means "start a
    # new session", same as an explicit POST /api/chat/sessions call would.
    session_id = (body.get("session_id") or "").strip() or str(uuid.uuid4())

    with db.SessionLocal() as session:
        # Oldest-first context window for Claude, scoped to THIS session
        # only (a new session must not silently inherit an old, unrelated
        # conversation's context) — capped so a long-lived chat doesn't grow
        # this call's token usage/latency unbounded.
        #
        # Extracted into plain dicts HERE, before session.commit() below —
        # commit() expires every object still attached to this session by
        # default, and the session itself closes when this `with` block
        # ends. run_chat_turn() runs well after that; handing it the raw
        # ChatMessage rows instead of plain data caused a
        # DetachedInstanceError the first time a conversation had any prior
        # history (never on a conversation's first message, which is why
        # this shipped unnoticed — see run_chat_turn()'s docstring).
        recent_messages = (
            session.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id, ChatMessage.actor == user, _message_cluster_filter(cluster))
            .order_by(ChatMessage.created_at.desc())
            .limit(MAX_HISTORY_MESSAGES)
            .all()
        )
        history = [
            {"role": m.role, "content": m.content}
            for m in reversed(recent_messages)
        ]
        previous = recent_messages[0] if recent_messages else None
        pending_node_command_id = (
            previous.id
            if previous is not None
            and previous.role == "assistant"
            and previous.proposed_action_id == "execute_node_command"
            and previous.proposed_status == "PENDING"
            else None
        )
        if mode == "delegate":
            # The lock and both ChatMessage/DelegatedAITask inserts must live
            # in one transaction. Checking counts in an earlier session lets
            # concurrent HTTP requests bypass the global/actor limits.
            _check_delegated_admission(session, user)
        user_message = ChatMessage(session_id=session_id, cluster_id=cluster.id, role="user", content=text, actor=user)
        session.add(user_message)
        session.flush()
        user_message_dict = _message_to_dict(user_message)
        if mode == "delegate":
            ai_name = auth.chat_ai_name(user)
            female_address = auth.chat_female_address(user)
            assistant_message = ChatMessage(
                session_id=session_id,
                cluster_id=cluster.id,
                role="assistant",
                content=with_romantic_address(
                    "Đã nhận việc. Supervisor đang tách thành các sub-agent độc lập; kết quả sẽ tự trả về trong phiên này.",
                    ai_name,
                    female_address,
                ),
                actor=user,
            )
            session.add(assistant_message)
            session.flush()
            delegated_task = DelegatedAITask(
                actor=user,
                cluster_id=cluster.id,
                session_id=session_id,
                assistant_message_id=assistant_message.id,
                prompt=text,
                status="QUEUED",
            )
            session.add(delegated_task)
            session.commit()
            session.refresh(assistant_message)
            session.refresh(delegated_task)
            assistant_message_dict = _message_to_dict(assistant_message)
            task_id = delegated_task.id
        else:
            session.commit()
            session.refresh(user_message)

    if mode == "delegate":
        try:
            await enqueue_task(task_id)
        except Exception as exc:
            logger.exception("could not enqueue delegated task %s", task_id)
            with db.SessionLocal() as session:
                task = session.get(DelegatedAITask, task_id)
                message = session.get(ChatMessage, assistant_message_dict["id"])
                if task is not None:
                    task.status = "FAILED"
                    task.error = "Không đưa được task vào hàng đợi xử lý"
                    task.finished_at = datetime.utcnow()
                if message is not None:
                    message.content = with_romantic_address(
                        "Không đưa được delegated task vào Worker; kiểm tra RabbitMQ/Worker.",
                        ai_name,
                        female_address,
                    )
                session.commit()
            raise HTTPException(status_code=503, detail="Worker chưa sẵn sàng nhận delegated task") from exc
        return {
            "mode": "delegate",
            "processing": True,
            "task_id": task_id,
            "user_message": user_message_dict,
            "assistant_message": assistant_message_dict,
        }

    if mode == "dual" and pending_node_command_id is None:
        # Return immediately. The background task persists each AI turn as it
        # finishes; the widget already polls /messages every 2.5 seconds, so
        # the operator sees the conversation incrementally instead of waiting
        # for all CLI calls to complete inside one HTTP request.
        key = _dual_job_key(user, cluster.id, session_id)
        task = asyncio.create_task(
            _run_dual_chat_background(text, history, session_id, cluster.id, user)
        )
        _DUAL_JOBS[key] = task
        task.add_done_callback(lambda done: _forget_dual_job(key, done))
        return {
            "mode": "dual",
            "user_message": user_message_dict,
            "assistant_messages": [],
            "processing": True,
        }


    if pending_node_command_id is not None:
        ai_name = auth.chat_ai_name(user)
        female_address = auth.chat_female_address(user)
        if text != "OK" or not auth.is_admin_user(user):
            with db.SessionLocal() as session:
                pending = session.get(ChatMessage, pending_node_command_id)
                if pending is not None and pending.proposed_status == "PENDING":
                    pending.proposed_status = "CANCELLED"
                assistant_message = ChatMessage(
                    session_id=session_id,
                    cluster_id=cluster.id,
                    role="assistant",
                    content=with_romantic_address(
                        "Đề xuất lệnh trên node đã huỷ vì tin nhắn kế tiếp không phải chính xác `OK`.",
                        ai_name,
                        female_address,
                    ),
                    actor=user,
                )
                session.add(assistant_message)
                session.commit()
                session.refresh(assistant_message)
                return {"user_message": user_message_dict, "assistant_message": _message_to_dict(assistant_message)}

        await _confirm_chat_action_core(pending_node_command_id, user, allow_node_command=True)
        from dashboard.routes.actions import approve_action_core
        with db.SessionLocal() as session:
            pending = session.get(ChatMessage, pending_node_command_id)
            action = session.query(Action).filter(Action.incident_id == pending.proposed_incident_id).one()
            action_id = action.id
        approve_action_core(action_id, user)
        with db.SessionLocal() as session:
            assistant_message = ChatMessage(
                session_id=session_id,
                cluster_id=cluster.id,
                role="assistant",
                content=with_romantic_address(
                    "Đã xác nhận `OK`. Lệnh đã được chuyển cho Worker thực hiện trên node đã chọn.",
                    ai_name,
                    female_address,
                ),
                actor=user,
            )
            session.add(assistant_message)
            session.commit()
            session.refresh(assistant_message)
            return {"user_message": user_message_dict, "assistant_message": _message_to_dict(assistant_message)}

    # Fails fast, before ever building a tool loop, with the exact sentinel
    # text dashboard/static/chat_widget.js matches on to render a clickable
    # "[Vào Cài đặt →]" link — the message bubble itself stays plain text
    # either way (no HTML in ChatMessage.content).
    api_ready = settings.router_enabled and settings.router_api_key and settings.router_base_url
    if not (settings.codex_chat_enabled or settings.claude_chat_enabled or api_ready):
        ai_name = auth.chat_ai_name(user)
        female_address = auth.chat_female_address(user)
        with db.SessionLocal() as session:
            assistant_message = ChatMessage(
                session_id=session_id,
                cluster_id=cluster.id,
                role="assistant",
                content=with_romantic_address(MISSING_AI_CONFIG_MESSAGE, ai_name, female_address),
                actor=user,
            )
            session.add(assistant_message)
            session.commit()
            session.refresh(assistant_message)
            assistant_message_dict = _message_to_dict(assistant_message)
        return {"user_message": user_message_dict, "assistant_message": assistant_message_dict}

    try:
        result = await run_chat_turn(history, ai_text, user, cluster)
    except ChatTurnError as exc:
        logger.warning("post_chat_message: %s", exc)
        ai_name = auth.chat_ai_name(user)
        female_address = auth.chat_female_address(user)
        with db.SessionLocal() as session:
            assistant_message = ChatMessage(
                session_id=session_id,
                cluster_id=cluster.id,
                role="assistant",
                content=with_romantic_address(str(exc), ai_name, female_address),
                actor=user,
            )
            session.add(assistant_message)
            session.commit()
            session.refresh(assistant_message)
            assistant_message_dict = _message_to_dict(assistant_message)
        return JSONResponse(
            status_code=502,
            content={
                "detail": str(exc),
                "user_message": user_message_dict,
                "assistant_message": assistant_message_dict,
            },
        )

    proposal = result["proposal"]
    tools_used = result.get("tools_used") or []
    with db.SessionLocal() as session:
        assistant_message = ChatMessage(
            session_id=session_id,
            cluster_id=cluster.id,
            role="assistant",
            content=result["reply_text"],
            actor=user,
            proposed_action_id=proposal["action_id"] if proposal else None,
            proposed_target_nodes=json.dumps(proposal["target_nodes"]) if proposal else None,
            proposed_action_params=(
                json.dumps(proposal["params"]) if proposal and proposal.get("params") else None
            ),
            proposed_rationale=proposal["rationale"] if proposal else None,
            proposed_command_preview=proposal.get("command_preview") if proposal else None,
            proposed_status="PENDING" if proposal else None,
            tools_used=json.dumps(tools_used) if tools_used else None,
        )
        session.add(assistant_message)
        session.commit()
        session.refresh(assistant_message)
        assistant_message_dict = _message_to_dict(assistant_message)

    return {"user_message": user_message_dict, "assistant_message": assistant_message_dict}


def _delegated_task_payload(session, task: DelegatedAITask) -> dict:
    subtasks = (
        session.query(DelegatedAISubtask)
        .filter_by(task_id=task.id)
        .order_by(DelegatedAISubtask.role)
        .all()
    )
    return {
        "task_id": task.id,
        "status": task.status,
        "result": task.result_text,
        "error": task.error,
        "subtasks": [
            {"id": item.id, "role": item.role, "status": item.status, "error": item.error}
            for item in subtasks
        ],
    }


@router.get("/api/chat/delegated-tasks")
async def list_delegated_tasks(
    request: Request,
    session_id: str = "",
    user: str = Depends(require_login),
):
    """Restore active delegated progress after a browser reload."""
    session_id = session_id.strip()
    if not session_id:
        return {"tasks": []}
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        tasks = (
            session.query(DelegatedAITask)
            .filter(
                DelegatedAITask.actor == user,
                DelegatedAITask.cluster_id == cluster.id,
                DelegatedAITask.session_id == session_id,
                DelegatedAITask.status.in_(_DELEGATED_ACTIVE_STATUSES),
            )
            .order_by(DelegatedAITask.created_at.asc())
            .limit(20)
            .all()
        )
        return {"tasks": [{"task_id": task.id, "status": task.status} for task in tasks]}


@router.get("/api/chat/delegated-tasks/{task_id}")
async def get_delegated_task(task_id: str, request: Request, user: str = Depends(require_login)):
    """Return progress only to the task owner and selected cluster."""
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        task = session.query(DelegatedAITask).filter_by(
            id=task_id, actor=user, cluster_id=cluster.id
        ).first()
        if task is None:
            raise HTTPException(status_code=404, detail="Delegated task không tồn tại")
        return _delegated_task_payload(session, task)


@router.post("/api/chat/delegated-tasks/{task_id}/cancel")
async def cancel_delegated_task(task_id: str, request: Request, user: str = Depends(require_login)):
    """Cancel an owned delegated task and prevent its Worker from progressing."""
    cluster = selected_cluster(request)
    with db.SessionLocal() as session:
        task = session.query(DelegatedAITask).filter_by(
            id=task_id, actor=user, cluster_id=cluster.id
        ).first()
        if task is None:
            raise HTTPException(status_code=404, detail="Delegated task không tồn tại")
        if task.status == "CANCELLED":
            return _delegated_task_payload(session, task)
        if task.status not in _DELEGATED_ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail=f"Task đã ở trạng thái {task.status}")

        result = session.execute(
            DelegatedAITask.__table__.update()
            .where(
                DelegatedAITask.id == task_id,
                DelegatedAITask.actor == user,
                DelegatedAITask.cluster_id == cluster.id,
                DelegatedAITask.status.in_(_DELEGATED_ACTIVE_STATUSES),
            )
            .values(
                status="CANCELLED",
                error="Đã hủy bởi operator",
                lease_until=None,
                finished_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        if result.rowcount != 1:
            session.rollback()
            raise HTTPException(status_code=409, detail="Task vừa chuyển sang trạng thái khác")
        session.refresh(task)
        session.execute(
            DelegatedAISubtask.__table__.update()
            .where(
                DelegatedAISubtask.task_id == task_id,
                DelegatedAISubtask.status.not_in(("COMPLETED", "FAILED", "CANCELLED")),
            )
            .values(
                status="CANCELLED",
                error="Sub-agent bị hủy theo delegated task",
                lease_until=None,
                finished_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        message = session.get(ChatMessage, task.assistant_message_id)
        if message is not None:
            message.content = with_romantic_address(
                "Đã hủy delegated task theo yêu cầu operator.",
                auth.chat_ai_name(user),
                auth.chat_female_address(user),
            )
        session.commit()
        session.refresh(task)
        return _delegated_task_payload(session, task)


@router.get("/api/chat/dual/status")
async def get_dual_chat_status(
    request: Request,
    session_id: str = "",
    user: str = Depends(require_login),
):
    raise HTTPException(status_code=404, detail="Chế độ Hai AI chỉ khả dụng qua Telegram Chatbox.")
    cluster = selected_cluster(request)
    key = _dual_job_key(user, cluster.id, session_id.strip()) if session_id.strip() else None
    task = _DUAL_JOBS.get(key) if key else None
    return {"session_id": session_id, "running": bool(task and not task.done())}


@router.post("/api/chat/dual/stop")
async def stop_dual_chat(request: Request, user: str = Depends(require_login)):
    raise HTTPException(status_code=404, detail="Chế độ Hai AI chỉ khả dụng qua Telegram Chatbox.")
    cluster = selected_cluster(request)
    body = await request.json()
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="Thiếu session_id của phiên Hai AI")
    key = _dual_job_key(user, cluster.id, session_id)
    task = _DUAL_JOBS.get(key)
    if task is None or task.done():
        _DUAL_JOBS.pop(key, None)
        return {"session_id": session_id, "stopped": False, "running": False}
    task.cancel()
    return {"session_id": session_id, "stopped": True, "running": False}


def _validate_chat_action_proposal(session, message):
    """Revalidate a staged chat proposal against the current cluster policy.

    Both simulation and confirmation call this function.  Keeping that
    decision boundary in one place is important: an operator must not see a
    different classification or command preview from the one that confirmation
    will actually persist.
    """
    action_id = message.proposed_action_id
    try:
        target_nodes = (
            json.loads(message.proposed_target_nodes) if message.proposed_target_nodes else None
        )
        action_params = (
            json.loads(message.proposed_action_params) if message.proposed_action_params else {}
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="Đề xuất không còn hợp lệ (dữ liệu bị lỗi)"
        ) from None

    is_management_action = action_id in VALID_MANAGEMENT_ACTION_IDS
    is_bluestore_action = action_id in VALID_BLUESTORE_ACTION_IDS
    is_parameterized_action = is_management_action or is_bluestore_action
    cluster = session.get(Cluster, message.cluster_id) if message.cluster_id else None
    if cluster is None:
        cluster = session.query(Cluster).filter_by(is_default=True).first()
    if cluster is None or not cluster.is_active:
        raise HTTPException(status_code=400, detail="Cụm của đề xuất không còn hoạt động")

    allowed_hosts = {node["host"] for node in configured_nodes(cluster)}
    if (
        action_id not in (VALID_ACTION_IDS | VALID_MANAGEMENT_ACTION_IDS | VALID_BLUESTORE_ACTION_IDS)
        or not isinstance(target_nodes, list)
        or not target_nodes
        or not all(isinstance(host, str) and host in allowed_hosts for host in target_nodes)
        # Management commands are cluster-wide (not per-host like
        # restart_osd_daemon/resync_ntp) — same single-node requirement
        # dashboard/chat_client.py::_validate_proposal enforces at proposal
        # time, re-checked here from scratch.
        or (is_parameterized_action and len(target_nodes) != 1)
        or not isinstance(action_params, dict)
    ):
        raise HTTPException(
            status_code=400,
            detail="Đề xuất không còn hợp lệ (action_id, node hoặc tham số đã thay đổi từ lúc đề xuất)",
        )

    resolved_command = message.proposed_command_preview
    if is_parameterized_action:
        # The command builder is the authoritative validation for pool name,
        # PG/size and OSD bounds.  Do not trust values saved when the proposal
        # was staged.
        try:
            resolved_command = executor_commands.get_command(
                action_id, target_nodes[0], action_params
            )
        except ExecutorError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Đề xuất không còn hợp lệ (tham số không hợp lệ: {exc})",
            ) from None

    # Pass the live session in both paths so persisted admin policy overrides
    # cannot make dry-run disagree with the resulting Action.
    classification = gate.classify_action(action_id, session=session)
    return action_id, target_nodes, action_params, cluster, resolved_command, classification


async def _confirm_chat_action_core(
    message_id: str, user: str, *, allow_node_command: bool = False
):
    """The ONLY place a chat conversation can turn into a real Incident/Action
    row — requires an explicit operator click (never triggered by Claude, see
    dashboard/chat_client.py's SYSTEM_PROMPT), and re-validates the proposal
    from scratch rather than trusting it was still safe/current since it was
    staged (settings or the node list may have changed in the meantime).

    Creates a synthetic Incident so the SAFE/RISKY Action pipeline that
    approval screen, the Worker's approved-action poller, the audit trail)
    handles everything from here on — this endpoint deliberately does not
    execute or SSH anywhere itself (AD-3: only the Worker holds SSH executor
    credentials).
    """
    with db.SessionLocal() as session:
        message = session.get(ChatMessage, message_id)
        if message is None or message.actor != user:
            raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn")
        if message.role != "assistant" or message.proposed_action_id is None:
            raise HTTPException(status_code=400, detail="Tin nhắn này không có đề xuất hành động")
        # A chat proposal can turn directly into an APPROVED SAFE action.
        # Owning the conversation is not sufficient authority to create a
        # cluster mutation: non-admin accounts may use Chat for observation,
        # but an admin must explicitly take responsibility for remediation.
        if not auth.is_admin_user(user):
            raise HTTPException(
                status_code=403,
                detail="Chỉ tài khoản admin được xác nhận hành động từ Chat",
            )
        if message.proposed_status != "PENDING":
            # Already confirmed (double-submit, second tab) — no-op, same
            # pattern as dashboard/routes/actions.py's approve/reject guard.
            return _message_to_dict(message)

        action_id = message.proposed_action_id
        if action_id == "execute_node_command" and not allow_node_command:
            raise HTTPException(
                status_code=400,
                detail="Lệnh trực tiếp trên node chỉ được xác nhận bằng cách nhập OK ở tin nhắn kế tiếp",
            )
        (
            action_id,
            target_nodes,
            action_params,
            cluster,
            resolved_command,
            classification,
        ) = _validate_chat_action_proposal(session, message)

        incident = Incident(
            cluster_id=cluster.id,
            ceph_code=CHAT_REQUEST_CEPH_CODE,
            status=IncidentStatus.NEW.value,
            log_excerpt=f"Yêu cầu qua Chat bởi {user}: {message.proposed_rationale or ''}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        is_safe = classification == ActionClassification.SAFE
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=classification.value,
            # SAFE means "no human approval needed" by the same definition
            # the Incident-triggered path already uses — status=APPROVED
            # here (rather than a fresh PENDING nothing polls) is what makes
            # the Worker's existing poll_approved_actions() pick this row up
            status=ActionStatus.APPROVED.value if is_safe else ActionStatus.PENDING_APPROVAL.value,
            rationale=message.proposed_rationale,
            target_nodes=json.dumps(target_nodes),
            action_params=json.dumps(action_params) if action_params else None,
            proposed_command=resolved_command,
        )
        session.add(action)
        session.flush()

        incident.status = (
            IncidentStatus.APPROVED.value if is_safe else IncidentStatus.PENDING_APPROVAL.value
        )

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_CHAT_ACTION_REQUESTED,
            actor=user,
        )
        if not is_safe:
            # Same event the Incident-triggered RISKY path fires
            # (worker/llm/router_client.py::_route_risky_to_approval) — this
            # Action now shows up on the Dashboard's existing "Chờ duyệt"
            # section exactly like any other RISKY action, actor=system
            # because routing-to-approval isn't itself an operator decision
            # (confirming the chat proposal was; that's the entry above).
            audit.record(
                session,
                incident_id=incident.id,
                action_id=action.id,
                event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
                actor=audit.ACTOR_SYSTEM,
            )

        message.proposed_status = "CONFIRMED"
        message.proposed_incident_id = incident.id
        session.commit()
        session.refresh(message)
        return _message_to_dict(message)


@router.post("/api/chat/messages/{message_id}/confirm-action")
async def confirm_chat_action(message_id: str, user: str = Depends(require_login)):
    return await _confirm_chat_action_core(message_id, user)


@router.post("/api/chat/messages/{message_id}/simulate-action")
async def simulate_chat_action(message_id: str, user: str = Depends(require_login)):
    """Build a deterministic, zero-side-effect remediation dry-run.

    This endpoint never opens SSH, calls Ceph, creates an Incident/Action,
    or publishes a queue message.  It is intentionally based on the same
    current proposal validation that confirmation uses, so the operator sees
    whether the staged request is still scoped to an active cluster before
    choosing the real confirmation path.
    """
    with db.SessionLocal() as session:
        message = session.get(ChatMessage, message_id)
        if message is None or message.actor != user:
            raise HTTPException(status_code=404, detail="Không tìm thấy tin nhắn")
        if message.role != "assistant" or message.proposed_action_id is None:
            raise HTTPException(status_code=400, detail="Tin nhắn này không có đề xuất hành động")
        if message.proposed_status != "PENDING":
            raise HTTPException(status_code=409, detail="Chỉ mô phỏng đề xuất đang chờ xác nhận")

        (
            action_id,
            target_nodes,
            _action_params,
            _cluster,
            command_preview,
            action_classification,
        ) = _validate_chat_action_proposal(session, message)
        classification = action_classification.value
        approval = {
            ActionClassification.READ_ONLY.value: "Không chạy thay đổi; chỉ đọc dữ liệu khi được xác nhận.",
            ActionClassification.SAFE.value: "Nếu xác nhận, Worker có thể thực hiện theo policy SAFE.",
            ActionClassification.RISKY.value: "Nếu xác nhận, action vẫn chờ operator duyệt qua Telegram.",
            ActionClassification.DESTRUCTIVE.value: "Nếu xác nhận, action vẫn chờ duyệt qua Telegram; xem kỹ tác động phá huỷ.",
        }[classification]
        return {
            "mode": "dry_run",
            "will_execute": False,
            "will_contact_cluster": False,
            "action_id": action_id,
            "classification": classification,
            "target_nodes": target_nodes,
            "command_preview": command_preview,
            "approval": approval,
            "steps": [
                {"step": "validate_proposal", "status": "passed", "detail": "Action, node và tham số còn hợp lệ."},
                {"step": "contact_cluster", "status": "skipped", "detail": "Mô phỏng không mở SSH và không gọi Ceph."},
                {
                    "step": "execute_action",
                    "status": "skipped",
                    "detail": "Không tạo Incident/Action, không gửi Worker hoặc RabbitMQ.",
                },
            ],
        }
