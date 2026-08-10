"""Category-scoped Telegram senders for Watcher-detected alerts (cluster
health/Incident errors, node hardware) — 2026-08-06: each category is now
its own fully independent Telegram channel with its OWN Bot Token/Chat ID
(previously all 3 categories shared one pair, switched on/off by a
separate per-category toggle). 2026-08-07: that per-category toggle is
back (see config/settings.py's `telegram_*_enabled` fields) — a channel is
only active when it's BOTH "configured" (token AND chat id non-blank) AND
`_enabled`, checked together in `_send` below.

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


def _with_cluster_prefix(text: str, cluster_name: str | None = None) -> str:
    """Prepends a cluster name as the first line of every message this
    module sends — lets an operator running several ceph-aiops instances
    (or, since 2026-08-07's multi-cluster observability Phase 1, several
    OBSERVED clusters from one instance) into the same Telegram chat tell
    which cluster an alert is from.

    `cluster_name=None` (every caller except send_incident_alert's observed-
    cluster case) falls back to `settings.cluster_name` — unchanged
    behavior for the default cluster. No-op (text unchanged) when the
    resolved name is blank, so a single-cluster deployment's messages stay
    byte-identical to before this existed."""
    name = (cluster_name if cluster_name is not None else settings.cluster_name).strip()
    if not name:
        return text
    return f"\U0001f4cd Cụm: {name}\n{text}"


def _send(bot_token: str, chat_id: str, enabled: bool, text: str, cluster_name: str | None = None) -> None:
    """`enabled` (2026-08-07, Alert Telegram page) is a SEPARATE on/off
    switch from "configured" (bot_token/chat_id both non-blank) — lets an
    operator pause a channel with one click without losing/retyping its
    Chat ID, unlike the earlier "blank the chat id to pause" design."""
    if not enabled or not bot_token or not chat_id:
        return
    try:
        send_telegram_message(bot_token, chat_id, _with_cluster_prefix(text, cluster_name))
    except TelegramSendError:
        logger.exception("shared.telegram_alerts: Telegram delivery failed")


def send_incident_alert(
    ceph_code: str, severity: str | None, log_excerpt: str | None, cluster_name: str | None = None
) -> None:
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
    _send(
        settings.telegram_incident_bot_token,
        settings.telegram_incident_chat_id,
        settings.telegram_incident_enabled,
        text,
        cluster_name,
    )


def send_node_alert(host: str, message: str) -> None:
    """Called once per NEWLY-flagged node resource problem
    (watcher/node_health_monitor.py::create_or_resolve_node_health_incidents
    — only when a new Incident is created, not resent on every scan a host
    stays flagged). No-op if the Phần cứng channel's bot token/chat id
    aren't configured yet (same reasoning as send_incident_alert above)."""
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, settings.telegram_node_enabled, f"\U0001f7e0 Phần cứng node {host}\n{message}")


def send_osd_latency_alert(osd_id: int, host: str | None, message: str) -> None:
    """Called once per NEWLY-flagged OSD latency outlier
    (watcher/osd_latency_monitor.py::create_or_resolve_osd_latency_incidents
    — only when a new Incident is created, same "one notification per
    genuinely new problem" posture as send_node_alert above). Shares the
    Phần cứng channel with send_node_alert — see this module's own
    docstring for why there's no separate 4th channel for this."""
    label = f"osd.{osd_id}" + (f" ({host})" if host else "")
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, settings.telegram_node_enabled, f"\U0001f7e0 OSD chậm bất thường: {label}\n{message}")


def send_crush_skew_alert(signal: str, entity_label: str, message: str) -> None:
    """Called once per NEWLY-flagged CRUSH data-distribution Skew
    (watcher/crush_skew_monitor.py::create_or_resolve_crush_skew_incidents —
    only when a new Incident is created, same "one notification per
    genuinely new problem" posture as send_osd_latency_alert above). Shares
    the Phần cứng channel with send_node_alert/send_osd_latency_alert (AD-31
    — the 2 Skew signals, USE and PG, are deliberately NOT a 4th channel).

    `entity_label` is a pre-formatted string (e.g. "osd.3" or "host node1")
    rather than separate osd_id/host parameters — the flagged entity can be
    either an OSD or a Host, and only one of those two ever has a
    meaningful osd_id, so a single already-formatted label avoids an
    always-None parameter on half of all calls."""
    label = f"{entity_label} ({signal})"
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, settings.telegram_node_enabled, f"\U0001f7e0 Lệch tải CRUSH: {label}\n{message}")


def send_database_size_alert(message: str) -> None:
    """Called once per NEWLY-flagged database-size Incident
    (watcher/database_capacity_monitor.py::create_or_resolve_database_size_incident
    — only when a new Incident is created, same "one notification per
    genuinely new problem" posture as send_node_alert/send_osd_latency_alert/
    send_crush_skew_alert above). Shares the Phần cứng channel with those 3
    — this is about a resource running out (the app's own DB storage), not
    a Ceph-cluster check, same reasoning AD-31 already established for not
    opening a 4th channel. No entity/label parameter -- there is only ever
    one database, unlike the per-osd/per-host/per-CRUSH-entity alerts above."""
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, settings.telegram_node_enabled, f"\U0001f7e0 Database ceph-aiops gần đầy\n{message}")


def send_auto_remediation_alert(
    ceph_code: str,
    diagnosis_text: str | None,
    rationale: str | None,
    command: str | None,
    succeeded: bool,
) -> None:
    """Called once per SAFE Action after execution finishes
    (worker/llm/router_client.py::_record_execution_result) — the
    send_incident_alert() call for the same Incident fires the moment it's
    CREATED, before the router has diagnosed anything, so it can only ever
    carry the raw ceph_code + log excerpt. This is the follow-up message
    that actually reports what the AI concluded and did about it, on the
    same Lỗi cụm channel (reusing telegram_incident_bot_token/chat_id
    rather than adding a 4th channel — same reasoning send_osd_latency_alert
    gives for reusing Phần cứng). No-op if that channel isn't configured,
    same as every other function in this module."""
    prefix = "\u2705 Đã tự động xử lý" if succeeded else "\u274c Tự động xử lý thất bại"
    lines = [f"{prefix}: {ceph_code}"]
    if diagnosis_text:
        lines.append(f"Chẩn đoán: {diagnosis_text}")
    if rationale:
        lines.append(f"Hành động: {rationale}")
    if command:
        lines.append(f"Lệnh đã chạy:\n{command}")
    _send(settings.telegram_incident_bot_token, settings.telegram_incident_chat_id, settings.telegram_incident_enabled, "\n".join(lines))
