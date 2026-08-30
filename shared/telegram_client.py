"""Telegram Bot API client.

Two families of calls here, split by direction:

- **Outbound-only** (`send_telegram_message`) — used by every alert
  category that never needs a reply: backup, cluster, hardware
  (worker/backup/alerting.py, shared/telegram_alerts.py, each with its own
  independent channel — config/settings.py), and the "Alert Telegram"
  page's "Gửi thử" test button. A message sent this way is purely
  informational; nothing reads a reply back.
- **Approval-request** (`send_telegram_message_with_keyboard`,
  `edit_telegram_message`, `get_telegram_updates`,
  `answer_telegram_callback`) — 2026-08-05, reworked 2026-08-06, backs
  dashboard/telegram_approval_bot.py's "Duyệt qua Telegram" feature — no
  longer a separate opt-in toggle, now a default capability of every one
  of the 3 alert channels above once configured (see that module's own
  docstring for the full design/trust-model). This IS the one place in
  this codebase that reads INCOMING Telegram updates
  (`get_telegram_updates`, Bot API long-polling `getUpdates`) — a
  deliberate, scoped exception to this module's own former "send-only"
  posture, added only because the operator explicitly asked for it.
  The Dashboard listener also enables `message` updates for the separate
  Chatbox AI Telegram channel; those are allow-listed and queued outside the
  polling thread. Even so, an incoming approval update can only ever trigger the SAME
  approve/reject logic the Dashboard's own buttons already call
  (worker/action_approval.py) — nothing new becomes executable that
  wasn't already one click away on the Dashboard.

Kept in shared/ (not worker/ or watcher/) so it stays importable from
either process without crossing any layering boundary, same posture as
shared/router_client.py.
"""

from __future__ import annotations

import logging
import re

import httpx

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_TIMEOUT_SECONDS = 10
TELEGRAM_TEXT_LIMIT = 4096
_TRUNCATION_SUFFIX = "\n… [đã rút gọn để phù hợp giới hạn Telegram]"
_CONTROL_TRANSLATION = {
    codepoint: " " for codepoint in (*range(0x20), 0x7F)
    if codepoint not in (0x09, 0x0A, 0x0D)
}
_SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"), "<TELEGRAM_BOT_TOKEN>"),
    (re.compile(r"\bAQ[A-Za-z0-9+/=]{20,}"), "<CEPHX_KEY>"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,20}\b"), "<AWS_ACCESS_KEY>"),
    (re.compile(r"(?i)\bauthorization\s*[:=]\s*[^\r\n]+"), "authorization: <REDACTED>"),
    (re.compile(r"(?i)\b(secret(?:_access)?_key|password|passwd|token)\s*[:=]\s*[^\s,;]+"), r"\1=<REDACTED>"),
    (re.compile(r"(?is)\s*\[SQL:\s*.*"), "\n[chi tiết SQL và parameters đã ẩn]"),
)


def sanitize_telegram_text(value: str | None, *, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Sanitize every operator-facing Telegram text at the final boundary."""
    text = (value or "").translate(_CONTROL_TRANSLATION)
    for pattern, replacement in _SECRET_REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        keep = max(0, limit - len(_TRUNCATION_SUFFIX))
        text = text[:keep].rstrip() + _TRUNCATION_SUFFIX
    return text


class _TelegramUrlRedactionFilter(logging.Filter):
    """Prevent httpx INFO logs from exposing Bot API tokens in URL paths."""

    _url_re = re.compile(r"/bot[^/\s]+/")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                self._url_re.sub("/bot<REDACTED>/", str(arg))
                if "api.telegram.org/bot" in str(arg) else arg
                for arg in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_TelegramUrlRedactionFilter())


class TelegramSendError(Exception):
    """Raised for any failure sending/editing a Telegram message, or
    answering a callback — missing config, a bad token/chat id, or a
    network/API failure. Callers decide whether to swallow this (best-
    effort alert delivery, same posture as the existing generic webhook in
    worker/backup/alerting.py) or surface it (the Settings page's "Gửi
    thử" button, where the operator needs a real error to know the config
    is wrong)."""


def _call_telegram_api(bot_token: str, method: str, payload: dict, *, timeout: float) -> dict:
    """Single choke point for every Telegram Bot API call in this module —
    every method (sendMessage/editMessageText/getUpdates/
    answerCallbackQuery) shares the exact same success/failure shape
    (`{"ok": true, "result": ...}` or `{"ok": false, "description": ...}`),
    so the "is this actually a success" check lives in exactly one place.
    Raises TelegramSendError on any failure — including a token/chat id
    that resolves to an API-level rejection (Telegram itself still
    responds HTTP 200 with `"ok": false` for some of those, so a bare
    raise_for_status() alone would miss them) — so a caller never mistakes
    a silently-rejected call for a successful one."""
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"
    try:
        response = httpx.post(url, json=payload, timeout=timeout)
    except Exception as exc:
        safe_error = sanitize_telegram_text(str(exc), limit=500)
        safe_error = safe_error.replace(bot_token, "<TELEGRAM_BOT_TOKEN>")
        raise TelegramSendError(f"Không gọi được Telegram API ({method}): {safe_error}") from exc

    try:
        body = response.json()
    except Exception:
        body = None

    if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok", False):
        description = (body or {}).get("description") if isinstance(body, dict) else None
        detail = sanitize_telegram_text(description or response.text, limit=500) or f"HTTP {response.status_code}"
        detail = detail.replace(bot_token, "<TELEGRAM_BOT_TOKEN>")
        raise TelegramSendError(f"Telegram API ({method}) từ chối: {detail}")
    return body


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    """POSTs `text` to `chat_id` via the Telegram Bot API's sendMessage.
    Plain text, no parse_mode — alert text here is built from dynamic,
    operator/AI-generated strings (error messages, log excerpts) that may
    contain '<'/'>'/'&'; asking Telegram to parse that as HTML/Markdown
    risks a 400 from a stray unescaped character, which is worse than
    losing italics/bold formatting."""
    if not bot_token or not chat_id:
        raise TelegramSendError("Chưa cấu hình Telegram bot token / chat id")
    _call_telegram_api(
        bot_token, "sendMessage", {"chat_id": chat_id, "text": sanitize_telegram_text(text)},
        timeout=TELEGRAM_TIMEOUT_SECONDS
    )


def set_telegram_commands(bot_token: str, commands: list[dict[str, str]]) -> None:
    """Register the slash-command menu shown by Telegram for this bot."""
    if not bot_token:
        raise TelegramSendError("Chưa cấu hình Telegram bot token")
    _call_telegram_api(
        bot_token,
        "setMyCommands",
        {"commands": commands},
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )


def send_telegram_message_with_keyboard(
    bot_token: str, chat_id: str, text: str, buttons: list[tuple[str, str]]
) -> int:
    """Same as send_telegram_message, plus a SINGLE ROW of inline buttons
    (`buttons` — list of (label, callback_data) pairs, e.g.
    `[("✅ Duyệt", "approve:<action_id>"), ("❌ Từ chối", "reject:<action_id>")]`).
    Returns the sent message's own `message_id` (Bot API `result.message_id`)
    — the caller (worker/action_approval.py's Telegram sender) must persist
    this on the Action row, since editing the message later (once a
    decision is made, from either Telegram or the Dashboard) needs it and
    happens in a completely separate poll cycle/process."""
    if not bot_token or not chat_id:
        raise TelegramSendError("Chưa cấu hình Telegram bot token / chat id")
    payload = {
        "chat_id": chat_id,
        "text": sanitize_telegram_text(text),
        "reply_markup": {
            "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
        },
    }
    body = _call_telegram_api(bot_token, "sendMessage", payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
    return int(body["result"]["message_id"])


def edit_telegram_message(bot_token: str, chat_id: str, message_id: int, text: str) -> None:
    """Replaces the text of an already-sent message and ALWAYS clears its
    inline keyboard (`reply_markup: {"inline_keyboard": []}`) — the only
    real use case in this codebase is "a decision was just made, show the
    outcome and remove the now-stale Duyệt/Từ chối buttons", never "edit
    but keep the buttons", so there's no separate buttons parameter to get
    wrong. Best-effort from the CALLER's side is expected here (a message
    that was already deleted/too old to edit is a real, harmless Telegram
    error — see worker/action_approval.py's own handling), not swallowed
    inside this function."""
    if not bot_token or not chat_id:
        raise TelegramSendError("Chưa cấu hình Telegram bot token / chat id")
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": sanitize_telegram_text(text),
        "reply_markup": {"inline_keyboard": []},
    }
    _call_telegram_api(bot_token, "editMessageText", payload, timeout=TELEGRAM_TIMEOUT_SECONDS)


def get_telegram_updates(
    bot_token: str,
    offset: int | None,
    timeout_seconds: int,
    allowed_updates: list[str] | None = None,
) -> list[dict]:
    """Long-polls Telegram's own `getUpdates` for up to `timeout_seconds`
    (Bot API holds the HTTP connection open until an update arrives or the
    timeout elapses — NOT a busy-poll) — the only inbound Telegram call in
    this codebase, scoped to `allowed_updates=["callback_query"]` only
    (this feature never needs to read plain chat messages, only inline-
    button presses by default; callers that also own a two-way chat may pass
    `['callback_query', 'message']`). `offset` should be the highest `update_id` already
    processed + 1 (Telegram's own documented ack mechanism — passing it
    tells the server it can stop re-delivering everything up to and
    including that id); `None` on the very first call.

    The HTTP client's own timeout is deliberately LARGER than
    `timeout_seconds` (+10s slack) — Telegram's long-poll response can
    legitimately take almost exactly `timeout_seconds` to arrive, and a
    tighter client-side timeout would spuriously abort a call that was
    about to succeed."""
    if not bot_token:
        raise TelegramSendError("Chưa cấu hình Telegram bot token")
    payload: dict = {
        "timeout": timeout_seconds,
        "allowed_updates": allowed_updates or ["callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    try:
        body = _call_telegram_api(
            bot_token, "getUpdates", payload, timeout=httpx.Timeout(timeout_seconds + 10)
        )
    except TelegramSendError as exc:
        # A long poll that reaches the client's read deadline has the same
        # meaning to this caller as Telegram returning an empty result: there
        # was no update to process during this cycle.  Keep all other network
        # and API failures visible so the listener can log and back off.
        if isinstance(exc.__cause__, httpx.ReadTimeout):
            return []
        raise
    result = body.get("result")
    return result if isinstance(result, list) else []


def answer_telegram_callback(bot_token: str, callback_query_id: str, text: str | None = None) -> None:
    """Acknowledges a `callback_query` (a button press) — Telegram shows
    the user a loading spinner on the button until this is called (or ~30s
    elapses and the client gives up on its own); `text` (if given) pops up
    as a small toast on the user's screen, used here to confirm "Đã duyệt"/
    "Đã từ chối" right at the moment they tap, before the message itself
    finishes being edited."""
    if not bot_token:
        raise TelegramSendError("Chưa cấu hình Telegram bot token")
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = sanitize_telegram_text(text, limit=200)
    _call_telegram_api(bot_token, "answerCallbackQuery", payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
