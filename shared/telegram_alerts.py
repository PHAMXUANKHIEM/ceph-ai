"""Category-scoped Telegram senders for Watcher-detected alerts (cluster
health/Incident errors, node hardware) — 2026-08-06: each category is now
its own fully independent Telegram channel with its OWN Bot Token/Chat ID
(previously all 3 categories shared one pair, switched on/off by a
separate per-category toggle). "Configured" (both token and chat id
non-blank) IS the on/off switch now — there is no separate enabled flag.

`send_osd_latency_alert` (2026-08-07, watcher/osd_latency_monitor.py)
deliberately reuses the SAME "Phần cứng" channel as `send_node_alert`
rather than getting its own 4th Bot Token/Chat ID pair — an OSD/disk
running abnormally slow is the same category of problem (physical
resource degradation) as a node's CPU/RAM being pegged, and the 3-channel
design is meant to stay exactly 3, not grow a new pair per alert type.

Kept in shared/ (not watcher/) so it stays importable from either process
without crossing any layering boundary, same posture as
shared/router_client.py/shared/telegram_client.py. Deliberately SEPARATE
from worker/backup/alerting.py::send_alert — that module owns its own
independent Backup channel config + webhook delivery, untouched by this
module.

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


def _send(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        return
    try:
        send_telegram_message(bot_token, chat_id, text)
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

    No-op if the Lỗi cụm channel's bot token/chat id aren't configured yet
    — checked here (not left to send_telegram_message's own "missing
    config" error) so an operator who simply hasn't set up this channel
    never sees a log entry about it "failing"."""
    prefix = _INCIDENT_SEVERITY_PREFIX.get(severity or "", f"⚠️ {severity or 'SỰ CỐ'}")
    excerpt = (log_excerpt or "").strip()
    if len(excerpt) > _MAX_EXCERPT_CHARS:
        excerpt = excerpt[:_MAX_EXCERPT_CHARS] + "…"
    text = f"{prefix} Cụm Ceph: {ceph_code}"
    if excerpt:
        text += f"\n{excerpt}"
    _send(settings.telegram_incident_bot_token, settings.telegram_incident_chat_id, text)


def send_node_alert(host: str, message: str) -> None:
    """Called once per NEWLY-flagged node resource problem
    (watcher/node_health_monitor.py::create_or_resolve_node_health_incidents
    — only when a new Incident is created, not resent on every scan a host
    stays flagged). No-op if the Phần cứng channel's bot token/chat id
    aren't configured yet (same reasoning as send_incident_alert above)."""
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, f"\U0001f7e0 Phần cứng node {host}\n{message}")


def send_osd_latency_alert(osd_id: int, host: str | None, message: str) -> None:
    """Called once per NEWLY-flagged OSD latency outlier
    (watcher/osd_latency_monitor.py::create_or_resolve_osd_latency_incidents
    — only when a new Incident is created, same "one notification per
    genuinely new problem" posture as send_node_alert above). Shares the
    Phần cứng channel with send_node_alert — see this module's own
    docstring for why there's no separate 4th channel for this."""
    label = f"osd.{osd_id}" + (f" ({host})" if host else "")
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, f"\U0001f7e0 OSD chậm bất thường: {label}\n{message}")
