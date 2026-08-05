"""Category-scoped Telegram senders for Watcher-detected alerts (cluster
health/Incident errors, node hardware) — "phân rõ cảnh báo Telegram theo
loại" feature: backup/cluster-lỗi/phần cứng must each be switchable
independently, never all-or-nothing.

Kept in shared/ (not watcher/) so it stays importable from either process
without crossing any layering boundary, same posture as
shared/router_client.py/shared/telegram_client.py. Deliberately SEPARATE
from worker/backup/alerting.py::send_alert — that module keeps its own
independent `telegram_alerts_enabled` toggle + webhook delivery for backup
alerts specifically, untouched by this module. All three categories
(backup here excluded) share the SAME Bot Token/Chat ID
(`settings.telegram_bot_token`/`telegram_chat_id`) — only the per-category
on/off switch differs, not the destination.

Best-effort like every other alert sender in this codebase: a
TelegramSendError here is logged and swallowed, never raised to the
caller — sending a notification must never fail whatever real check/scan
triggered it.
"""

from __future__ import annotations

import logging

from config.settings import settings
from shared.telegram_client import TelegramSendError, send_telegram_message

logger = logging.getLogger(__name__)

# Truncation length for a ceph_code's log_excerpt — some (e.g. a PG dump or
# a slow-ops list) can run to several KB, well past what's useful to read
# in a phone notification, and needlessly close to Telegram's own 4096-char
# message limit once the rest of the text is added.
_MAX_EXCERPT_CHARS = 800

_INCIDENT_SEVERITY_PREFIX = {
    "HEALTH_ERR": "\U0001f534 HEALTH_ERR",  # red circle
    "HEALTH_WARN": "\U0001f7e1 HEALTH_WARN",  # yellow circle
}


def _send(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        send_telegram_message(settings.telegram_bot_token, settings.telegram_chat_id, text)
    except TelegramSendError:
        logger.exception("shared.telegram_alerts: Telegram delivery failed")


def send_incident_alert(ceph_code: str, severity: str | None, log_excerpt: str | None) -> None:
    """Called once per newly-created cluster-health Incident
    (watcher/main.py::build_and_publish_incident, one call per `ceph
    health detail` check) — a genuine cluster problem, NOT a Volume-
    saturation/DeviceHealth-prediction Incident (those are their own
    ceph_code families with their own create/resolve lifecycle and are
    deliberately out of scope for this function; a raw `ceph health
    detail` check code is always what reaches this function via the one
    call site above).

    No-op if `settings.telegram_incident_alerts_enabled` is off, or if
    bot token/chat id aren't configured yet — checked here (not left to
    send_telegram_message's own "missing config" error) so an operator who
    simply hasn't turned this category on never sees a log entry about it
    "failing"."""
    if not settings.telegram_incident_alerts_enabled:
        return
    prefix = _INCIDENT_SEVERITY_PREFIX.get(severity or "", f"⚠️ {severity or 'SỰ CỐ'}")
    excerpt = (log_excerpt or "").strip()
    if len(excerpt) > _MAX_EXCERPT_CHARS:
        excerpt = excerpt[:_MAX_EXCERPT_CHARS] + "…"
    text = f"{prefix} Cụm Ceph: {ceph_code}"
    if excerpt:
        text += f"\n{excerpt}"
    _send(text)


def send_node_alert(host: str, message: str) -> None:
    """Called once per NEWLY-flagged node resource problem
    (watcher/node_health_monitor.py::create_or_resolve_node_health_incidents
    — only when a new Incident is created, not resent on every scan a host
    stays flagged). No-op if `settings.telegram_node_alerts_enabled` is
    off, or bot token/chat id aren't configured yet (same reasoning as
    send_incident_alert above)."""
    if not settings.telegram_node_alerts_enabled:
        return
    _send(f"\U0001f7e0 Phần cứng node {host}\n{message}")
