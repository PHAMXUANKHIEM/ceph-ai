"""Telegram-based "Duyệt"/"Từ chối" for PENDING_APPROVAL actions
(2026-08-05) — the 4th, most powerful Telegram category: unlike Backup/
Cảnh báo lỗi cụm/Phần cứng (all pure notifications), this one lets an
operator actually resolve an Action from their phone, via an inline
keyboard, without opening the Dashboard.

Lives in dashboard/ (NOT worker/ or watcher/) — the mutual-exclusion
checks `approve_action_core` needs (a cluster upgrade or patch install
in flight) live in `dashboard/routes/upgrade.py`/`dashboard/routes/
patch.py`, and Worker/Watcher must never import from dashboard/ (AD-3's
layering — the reverse direction, dashboard importing worker, is
already fine and used elsewhere). Keeping this in dashboard/ avoids ever
needing to relocate those checks.

Two independent background DAEMON THREADS (`start()`, called once from
`dashboard/app.py`'s lifespan startup — never from a request handler,
must survive the whole process lifetime):

1. `_notify_loop` — short cadence
   (`settings.telegram_approval_scan_interval_seconds`, default 10s): scans
   for `Action` rows at `PENDING_APPROVAL` never yet sent to Telegram
   (`telegram_notified_at IS NULL`), sends each with a "✅ Duyệt"/"❌ Từ
   chối" inline keyboard, and stamps `telegram_message_id`/
   `telegram_notified_at` so it's never sent twice — including across a
   Dashboard restart, since that state lives in the DB, not in memory.
2. `_listen_loop` — Telegram Bot API long-polling (`getUpdates`), the one
   place in this whole codebase that reads INCOMING Telegram messages.
   For each `callback_query` (a button press): verifies the chat_id
   matches configured config (see TRUST MODEL below), calls the EXACT
   SAME `approve_action_core`/`reject_action_core`
   (`dashboard/routes/actions.py`) the Dashboard's own HTML buttons use —
   no second implementation to drift — then edits the original message to
   show the outcome and answers the callback.

Both threads check `settings.telegram_approval_requests_enabled` (plus
bot token/chat id) FRESH on every loop iteration before ever touching the
network — toggling the Settings checkbox takes effect on the very next
iteration, no Dashboard restart required (this config lives IN this same
process, unlike the Worker/Watcher-side Telegram toggles).

TRUST MODEL — read before enabling this: authorization here is "did this
button press come from `settings.telegram_chat_id`", the SAME identity
already used to decide whether ANY alert is delivered there at all. If
that chat is a private 1:1 chat with one trusted operator, approving from
Telegram is exactly as strong as that operator's own Telegram account. If
it's a GROUP chat with several people, EVERYONE in that group can
approve/reject any RISKY action currently pending — same trust boundary
as sharing Dashboard login credentials, just a different set of people.
There is NO mapping anywhere in this codebase from a Telegram account to a
specific Dashboard user — the audit trail records `actor="telegram:<username-or-id>"`,
never a real Dashboard username, when a decision comes from here.

Deliberately synchronous throughout (shared/telegram_client.py's blocking
httpx calls, unchanged) — both loops run entirely off FastAPI's own
asyncio event loop, in their own dedicated threads, so a ~30s Telegram
long-poll response never stalls a single Dashboard HTTP request.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from config.settings import settings
from dashboard.routes.actions import (
    ActionConflictError,
    ActionNotFoundError,
    ApprovalOutcome,
    approve_action_core,
    reject_action_core,
)
from shared import db
from shared.models import Action, ActionStatus
from shared.telegram_client import (
    TelegramSendError,
    answer_telegram_callback,
    edit_telegram_message,
    get_telegram_updates,
    send_telegram_message_with_keyboard,
)

logger = logging.getLogger(__name__)

APPROVE_CALLBACK_PREFIX = "approve:"
REJECT_CALLBACK_PREFIX = "reject:"
# Telegram holds the getUpdates HTTP connection open for up to this long
# waiting for a new update — this IS the loop's own pacing, no separate
# sleep needed between iterations while updates keep arriving.
_LONG_POLL_TIMEOUT_SECONDS = 30
# Backoff between iterations when the feature is off/unconfigured, or
# after an API error — deliberately short (a Settings toggle flip should
# take effect quickly) but not zero (must never busy-loop).
_IDLE_BACKOFF_SECONDS = 5


def _configured() -> bool:
    return bool(
        settings.telegram_approval_requests_enabled
        and settings.telegram_bot_token
        and settings.telegram_chat_id
    )


def _action_message_text(action: Action) -> str:
    lines = [f"📋 Đề xuất chờ duyệt: {action.action_id}"]
    if action.rationale:
        lines.append(action.rationale)
    if action.proposed_command:
        lines.append(f"\nLệnh xem trước:\n{action.proposed_command}")
    lines.append(f"\nAction ID: {action.id}")
    return "\n".join(lines)


def _keyboard_for(action_id: str) -> list[tuple[str, str]]:
    return [
        ("✅ Duyệt", f"{APPROVE_CALLBACK_PREFIX}{action_id}"),
        ("❌ Từ chối", f"{REJECT_CALLBACK_PREFIX}{action_id}"),
    ]


def _notify_pending_actions() -> None:
    """One scan cycle. Reads the candidate id list in one query, then
    re-checks + sends + stamps EACH Action in its own session/transaction
    — a failure sending one candidate must not lose progress already made
    on the others in this same scan, and a status change that happened
    between the initial query and this row's turn (approved/rejected via
    the Dashboard in the meantime) must be re-checked fresh, not assumed
    still true from the initial query."""
    with db.SessionLocal() as session:
        candidate_ids = [
            row.id
            for row in session.query(Action.id)
            .filter(Action.status == ActionStatus.PENDING_APPROVAL.value)
            .filter(Action.telegram_notified_at.is_(None))
            .all()
        ]

    for action_id in candidate_ids:
        with db.SessionLocal() as session:
            action = session.get(Action, action_id)
            if action is None or action.status != ActionStatus.PENDING_APPROVAL.value:
                continue
            if action.telegram_notified_at is not None:
                continue
            try:
                message_id = send_telegram_message_with_keyboard(
                    settings.telegram_bot_token,
                    settings.telegram_chat_id,
                    _action_message_text(action),
                    _keyboard_for(action.id),
                )
            except TelegramSendError:
                logger.exception(
                    "telegram_approval_bot: failed to send approval request for action %s", action_id
                )
                continue
            action.telegram_message_id = message_id
            action.telegram_notified_at = datetime.utcnow()
            session.commit()


def _notify_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        if _configured():
            try:
                _notify_pending_actions()
            except Exception:
                logger.exception("telegram_approval_bot: _notify_pending_actions crashed")
        stop_event.wait(max(1, settings.telegram_approval_scan_interval_seconds))


_OUTCOME_EDIT_SUFFIX = {
    ApprovalOutcome.APPROVED: "\n\n✅ ĐÃ DUYỆT.",
    ApprovalOutcome.ACKNOWLEDGED: "\n\n✅ Đã xác nhận (không có lệnh tự động để chạy cho mục này).",
    ApprovalOutcome.REJECTED: "\n\n❌ ĐÃ TỪ CHỐI.",
    ApprovalOutcome.ALREADY_HANDLED: "\n\n⚠️ Đã được xử lý từ trước (có thể qua Dashboard).",
}
_OUTCOME_TOAST = {
    ApprovalOutcome.APPROVED: "Đã duyệt",
    ApprovalOutcome.ACKNOWLEDGED: "Đã xác nhận",
    ApprovalOutcome.REJECTED: "Đã từ chối",
    ApprovalOutcome.ALREADY_HANDLED: "Đã được xử lý từ trước",
}


def _actor_for(callback_query: dict) -> str:
    sender = callback_query.get("from") or {}
    username = sender.get("username")
    user_id = sender.get("id")
    return f"telegram:{username or user_id or 'unknown'}"


def _handle_callback_query(callback_query: dict) -> None:
    data = callback_query.get("data") or ""
    callback_id = callback_query.get("id")
    message = callback_query.get("message") or {}
    message_id = message.get("message_id")
    incoming_chat_id = str((message.get("chat") or {}).get("id", ""))

    # TRUST MODEL — see this module's own docstring: only a button press
    # coming from the configured chat is ever honored.
    if not settings.telegram_chat_id or incoming_chat_id != str(settings.telegram_chat_id):
        logger.warning(
            "telegram_approval_bot: ignoring callback_query from unrecognized chat_id=%s",
            incoming_chat_id,
        )
        if callback_id:
            try:
                answer_telegram_callback(settings.telegram_bot_token, callback_id, "Không có quyền")
            except TelegramSendError:
                pass
        return

    if data.startswith(APPROVE_CALLBACK_PREFIX):
        action_id = data[len(APPROVE_CALLBACK_PREFIX):]
        core_fn = approve_action_core
    elif data.startswith(REJECT_CALLBACK_PREFIX):
        action_id = data[len(REJECT_CALLBACK_PREFIX):]
        core_fn = reject_action_core
    else:
        logger.warning("telegram_approval_bot: unrecognized callback_data=%r", data)
        return

    actor = _actor_for(callback_query)
    try:
        result = core_fn(action_id, actor)
        edit_suffix = _OUTCOME_EDIT_SUFFIX[result.outcome]
        toast = _OUTCOME_TOAST[result.outcome]
    except ActionNotFoundError:
        edit_suffix = "\n\n⚠️ Không tìm thấy đề xuất này (có thể đã bị xoá)."
        toast = "Không tìm thấy đề xuất này"
    except ActionConflictError as exc:
        edit_suffix = f"\n\n⚠️ {exc.detail}"
        toast = exc.detail

    if callback_id:
        try:
            answer_telegram_callback(settings.telegram_bot_token, callback_id, toast)
        except TelegramSendError:
            logger.exception("telegram_approval_bot: answerCallbackQuery failed")

    if message_id is not None:
        with db.SessionLocal() as session:
            action = session.get(Action, action_id)
            base_text = _action_message_text(action) if action is not None else f"Action ID: {action_id}"
        try:
            edit_telegram_message(
                settings.telegram_bot_token, settings.telegram_chat_id, message_id, base_text + edit_suffix
            )
        except TelegramSendError:
            logger.exception("telegram_approval_bot: failed to edit message %s after decision", message_id)


def _listen_loop(stop_event: threading.Event) -> None:
    offset: int | None = None
    while not stop_event.is_set():
        if not _configured():
            stop_event.wait(_IDLE_BACKOFF_SECONDS)
            continue
        try:
            updates = get_telegram_updates(settings.telegram_bot_token, offset, _LONG_POLL_TIMEOUT_SECONDS)
        except TelegramSendError:
            logger.exception("telegram_approval_bot: getUpdates failed")
            stop_event.wait(_IDLE_BACKOFF_SECONDS)
            continue

        for update in updates:
            offset = max(offset or 0, update.get("update_id", 0) + 1)
            callback_query = update.get("callback_query")
            if not callback_query:
                continue
            try:
                _handle_callback_query(callback_query)
            except Exception:
                logger.exception("telegram_approval_bot: unexpected error handling callback_query")


_threads: list[threading.Thread] = []
_stop_event = threading.Event()


def start() -> None:
    """Called once from `dashboard/app.py`'s lifespan startup. Idempotent
    — a second call (e.g. FastAPI's TestClient re-entering the app's
    lifespan across many tests within one process, or an app reload) is a
    no-op; both threads are daemon=True so they never block process exit
    and need no explicit shutdown hook."""
    if _threads:
        return
    for target in (_notify_loop, _listen_loop):
        thread = threading.Thread(target=target, args=(_stop_event,), daemon=True)
        thread.start()
        _threads.append(thread)
