"""Telegram Bot API client — OUTBOUND notifications only.

Same "shared/, importable from both Dashboard and Worker without crossing
AD-3's layering" posture as shared/router_client.py: dashboard/routes/
settings.py's "Gửi thử" test-send button and worker/backup/alerting.py's
real fail/overdue/AI-analysis alerts both call the one function below,
neither reimplements the HTTP call.

Deliberately send-only, by design, not by omission: this project has no
Telegram bot LISTENING for messages (no long-polling, no incoming webhook
receiver anywhere in the codebase). A message sent from here is purely
informational — nothing reads a reply back, and no text typed into
Telegram can ever reach worker/executor/ or trigger a Command. Every
remediation action still goes through the existing Incident/Action
propose-then-approve flow (dashboard/routes/actions.py) exactly as before;
Telegram only gets to know about it after a human decides here, not the
other way around.
"""

from __future__ import annotations

import httpx

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_TIMEOUT_SECONDS = 10


class TelegramSendError(Exception):
    """Raised for any failure sending a Telegram message — missing
    config, a bad token/chat id, or a network/API failure. Callers decide
    whether to swallow this (best-effort alert delivery, same posture as
    the existing generic webhook in worker/backup/alerting.py) or surface
    it (the Settings page's "Gửi thử" button, where the operator needs a
    real error to know the config is wrong)."""


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    """POSTs `text` to `chat_id` via the Telegram Bot API's sendMessage.
    Plain text, no parse_mode — alert text here is built from dynamic,
    operator/AI-generated strings (error messages, log excerpts) that may
    contain '<'/'>'/'&'; asking Telegram to parse that as HTML/Markdown
    risks a 400 from a stray unescaped character, which is worse than
    losing italics/bold formatting.

    Raises TelegramSendError on any failure — including a token/chat id
    that resolves to an API-level rejection (Telegram itself still
    responds 200 with `"ok": false` for some of those, so a bare
    raise_for_status() alone would miss them) — so a caller never mistakes
    a silently-rejected message for a delivered one."""
    if not bot_token or not chat_id:
        raise TelegramSendError("Chưa cấu hình Telegram bot token / chat id")

    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    try:
        response = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text},
            timeout=TELEGRAM_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise TelegramSendError(f"Không gửi được tới Telegram: {exc}") from exc

    try:
        body = response.json()
    except Exception:
        body = None

    if response.status_code != 200 or not isinstance(body, dict) or not body.get("ok", False):
        description = (body or {}).get("description") if isinstance(body, dict) else None
        detail = description or response.text or f"HTTP {response.status_code}"
        raise TelegramSendError(f"Telegram API từ chối: {detail}")
