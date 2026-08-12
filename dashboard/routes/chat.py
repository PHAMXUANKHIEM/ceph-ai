import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from config.settings import settings
from dashboard.chat_client import (
    ChatTurnError,
    MAX_HISTORY_MESSAGES,
    MISSING_AI_CONFIG_MESSAGE,
    run_chat_turn,
    with_romantic_address,
)
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.vntime import to_utc_iso
from shared import audit, db
from shared.cluster_nodes import configured_nodes
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    ChatMessage,
    ChatPreference,
    Incident,
    IncidentStatus,
)
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.llm.router_client import VALID_ACTION_IDS
from worker.policy import gate
from worker.policy.gate import VALID_BLUESTORE_ACTION_IDS, VALID_MANAGEMENT_ACTION_IDS

logger = logging.getLogger(__name__)

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


@router.get("/api/chat/preferences")
async def get_chat_preferences(user: str = Depends(require_login)):
    return {"ai_name": auth.chat_ai_name(user)}


@router.put("/api/chat/preferences")
async def update_chat_preferences(request: Request, user: str = Depends(require_login)):
    body = await request.json()
    ai_name = _validated_ai_name(body.get("ai_name"))
    with db.SessionLocal() as session:
        preference = session.get(ChatPreference, user)
        if preference is None:
            preference = ChatPreference(username=user, ai_name=ai_name)
            session.add(preference)
        else:
            preference.ai_name = ai_name
        session.commit()
    return {"ai_name": ai_name}


def _message_to_dict(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "session_id": message.session_id,
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


_NO_MESSAGES_YET = object()  # sentinel — distinct from "the latest row's session_id happens to be None"


def _latest_session_id(session, actor: str):
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
    latest = (
        session.query(ChatMessage)
        .filter(ChatMessage.actor == actor)
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


def _build_session_summaries(session, actor: str) -> list[dict]:
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
    rows = (
        session.query(ChatMessage)
        .filter(ChatMessage.actor == actor)
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
async def list_chat_sessions(user: str = Depends(require_login)):
    """Powers the per-login chat history — one row per past conversation,
    most-recently-active first, so the operator can browse or delete an old
    session without it ever having to be "the current one" again."""
    with db.SessionLocal() as session:
        summaries = _build_session_summaries(session, user)
        current = _latest_session_id(session, user)
        for entry in summaries:
            entry["is_current"] = entry["session_id"] == current
        return {"sessions": summaries}


@router.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str, user: str = Depends(require_login)):
    """Permanently deletes every message in one session — the only
    destructive endpoint across Story 6.1-6.3 (everything else is additive
    or reversible-in-spirit, e.g. starting a new session doesn't delete the
    old one). Does NOT touch any Incident/Action a confirmed proposal in
    this session may have created — those are independent, real
    infrastructure state (Story 6.1's confirm-action already copied
    everything the Worker/approval pipeline needs into their own rows), and
    nothing in this schema has a foreign key pointing AT ChatMessage.id, so
    deleting these rows can never leave anything else dangling.
    """
    with db.SessionLocal() as session:
        deleted = (
            session.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id, ChatMessage.actor == user)
            .delete()
        )
        session.commit()
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Không tìm thấy đoạn chat")
    return {"deleted": deleted}


@router.get("/api/chat/messages")
async def get_chat_messages(user: str = Depends(require_login)):
    """Backs the floating chat widget (dashboard/static/chat_widget.js) on
    page load — the widget has no server-rendered history of its own (it's
    embedded directly in dashboard/templates/index.html, not its own route/
    template), so it fetches the recent transcript over this endpoint
    instead. Scoped to the CURRENT session only (_latest_session_id) — older
    sessions still exist in the DB, just not shown once a newer one has a
    message in it."""
    with db.SessionLocal() as session:
        session_id = _latest_session_id(session, user)
        if session_id is _NO_MESSAGES_YET:
            return {"messages": [], "session_id": None}
        rows = (
            session.query(ChatMessage)
            .filter(ChatMessage.session_id == session_id, ChatMessage.actor == user)
            .order_by(ChatMessage.created_at.asc())
            .limit(CHAT_WIDGET_HISTORY_LIMIT)
            .all()
        )
        return {"messages": [_message_to_dict(m) for m in rows], "session_id": session_id}


@router.post("/api/chat/messages")
async def post_chat_message(request: Request, user: str = Depends(require_login)):
    body = await request.json()
    text = (body.get("content") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Nội dung tin nhắn không được để trống")
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
        history = [
            {"role": m.role, "content": m.content}
            for m in reversed(
                session.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id, ChatMessage.actor == user)
                .order_by(ChatMessage.created_at.desc())
                .limit(MAX_HISTORY_MESSAGES)
                .all()
            )
        ]
        user_message = ChatMessage(session_id=session_id, role="user", content=text, actor=user)
        session.add(user_message)
        session.commit()
        session.refresh(user_message)
        user_message_dict = _message_to_dict(user_message)

    # Fails fast, before ever building a tool loop, with the exact sentinel
    # text dashboard/static/chat_widget.js matches on to render a clickable
    # "[Vào Cài đặt →]" link — the message bubble itself stays plain text
    # either way (no HTML in ChatMessage.content).
    api_ready = settings.router_enabled and settings.router_api_key and settings.router_base_url
    if not (settings.codex_chat_enabled or settings.claude_chat_enabled or api_ready):
        ai_name = auth.chat_ai_name(user)
        with db.SessionLocal() as session:
            assistant_message = ChatMessage(
                session_id=session_id,
                role="assistant",
                content=with_romantic_address(MISSING_AI_CONFIG_MESSAGE, ai_name),
                actor=user,
            )
            session.add(assistant_message)
            session.commit()
            session.refresh(assistant_message)
            assistant_message_dict = _message_to_dict(assistant_message)
        return {"user_message": user_message_dict, "assistant_message": assistant_message_dict}

    try:
        result = await run_chat_turn(history, text, user)
    except ChatTurnError as exc:
        logger.warning("post_chat_message: %s", exc)
        ai_name = auth.chat_ai_name(user)
        with db.SessionLocal() as session:
            assistant_message = ChatMessage(
                session_id=session_id,
                role="assistant",
                content=with_romantic_address(str(exc), ai_name),
                actor=user,
            )
            session.add(assistant_message)
            session.commit()
            session.refresh(assistant_message)
            assistant_message_dict = _message_to_dict(assistant_message)
        return {"user_message": user_message_dict, "assistant_message": assistant_message_dict}

    proposal = result["proposal"]
    tools_used = result.get("tools_used") or []
    with db.SessionLocal() as session:
        assistant_message = ChatMessage(
            session_id=session_id,
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


@router.post("/api/chat/messages/{message_id}/confirm-action")
async def confirm_chat_action(message_id: str, user: str = Depends(require_login)):
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
        if message.proposed_status != "PENDING":
            # Already confirmed (double-submit, second tab) — no-op, same
            # pattern as dashboard/routes/actions.py's approve/reject guard.
            return _message_to_dict(message)

        action_id = message.proposed_action_id
        try:
            target_nodes = (
                json.loads(message.proposed_target_nodes) if message.proposed_target_nodes else None
            )
        except (TypeError, ValueError):
            target_nodes = None
        try:
            action_params = (
                json.loads(message.proposed_action_params) if message.proposed_action_params else None
            )
        except (TypeError, ValueError):
            action_params = None

        is_management_action = action_id in VALID_MANAGEMENT_ACTION_IDS
        is_bluestore_action = action_id in VALID_BLUESTORE_ACTION_IDS
        is_parameterized_action = is_management_action or is_bluestore_action
        allowed_hosts = {n["host"] for n in configured_nodes()}
        if (
            action_id
            not in (VALID_ACTION_IDS | VALID_MANAGEMENT_ACTION_IDS | VALID_BLUESTORE_ACTION_IDS)
            or not isinstance(target_nodes, list)
            or not target_nodes
            or not all(isinstance(host, str) and host in allowed_hosts for host in target_nodes)
            # Management commands are cluster-wide (not per-host like
            # restart_osd_daemon/resync_ntp) — same single-node requirement
            # dashboard/chat_client.py::_validate_proposal already enforces
            # at proposal time, re-checked here from scratch.
            or (is_parameterized_action and len(target_nodes) != 1)
        ):
            raise HTTPException(
                status_code=400,
                detail="Đề xuất không còn hợp lệ (action_id hoặc node đã thay đổi từ lúc đề xuất)",
            )

        resolved_command = message.proposed_command_preview
        if is_parameterized_action:
            # Authoritative re-validation of action_params (pool name
            # charset, pg_num/size/osd_id bounds — see
            # worker/executor/commands.py's builders) from scratch at
            # confirm time, same "don't trust what was staged" posture this
            # endpoint already applies to action_id/target_nodes above.
            try:
                resolved_command = executor_commands.get_command(
                    action_id, target_nodes[0], action_params or {}
                )
            except ExecutorError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Đề xuất không còn hợp lệ (tham số không hợp lệ: {exc})",
                )

        incident = Incident(
            ceph_code=CHAT_REQUEST_CEPH_CODE,
            status=IncidentStatus.NEW.value,
            log_excerpt=f"Yêu cầu qua Chat bởi {user}: {message.proposed_rationale or ''}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        classification = gate.classify_action(action_id)
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
