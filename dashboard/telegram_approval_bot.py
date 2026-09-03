"""Telegram-based "Duyệt"/"Từ chối" for PENDING_APPROVAL actions
(2026-08-05, reworked 2026-08-06 for 3 independent channels) — unlike a
pure notification, this lets an operator actually resolve an Action from
their phone, via an inline keyboard, without opening the Dashboard.

2026-08-06: there is no longer a single shared Bot Token/Chat ID with its
own separate "phê duyệt" toggle. Backup/Lỗi cụm/Phần cứng are now 3
independent Telegram channels (config/settings.py), each with its own Bot
Token + Chat ID, and Duyệt/Từ chối is a DEFAULT capability of EVERY one of
them: a PENDING_APPROVAL Action's request is BROADCAST to every channel
that has a token+chat id configured, simultaneously — approving/rejecting
from any one of them resolves the Action everywhere. Since 2026-08-11,
action families may narrow that legacy broadcast via `channels_for_action`
(Volume Performance routes only to Lỗi cụm/Incident). There is no separate
on/off switch for this — "a channel is configured" IS "that channel can
approve/reject".

Lives in dashboard/ (NOT worker/ or watcher/) — the mutual-exclusion
checks `approve_action_core` needs (a cluster upgrade or patch install
in flight) live in `dashboard/routes/upgrade.py`/`dashboard/routes/
patch.py`, and Worker/Watcher must never import from dashboard/ (AD-3's
layering — the reverse direction, dashboard importing worker, is
already fine and used elsewhere). Keeping this in dashboard/ avoids ever
needing to relocate those checks.

Two kinds of background DAEMON THREADS, started once from
`dashboard/app.py`'s lifespan startup (`start()`, never from a request
handler, must survive the whole process lifetime):

1. `_notify_loop` — short cadence
   (`settings.telegram_approval_scan_interval_seconds`, default 10s): scans
   every `PENDING_APPROVAL` Action and, for each channel currently
   configured that this Action hasn't been sent to yet (tracked in
   `Action.telegram_message_ids`, a JSON dict of {channel_key: message_id}),
   sends a "✅ Duyệt"/"❌ Từ chối" inline-keyboard message to THAT channel
   and records its message id. A channel that fails to send (bad token,
   bot not in that chat, network) is simply retried on the next scan —
   it never blocks the other channels for the same Action. A channel
   configured/fixed AFTER an Action was created still picks it up on the
   very next scan (no restart needed, no "too late" window).
2. `_listen_supervisor_loop` + one `_listen_loop_for_token` thread PER
   UNIQUE bot token currently in use — Telegram's `getUpdates` long-poll
   and its `offset` ack are scoped to a BOT TOKEN, not to a chat, so if two
   channels happen to share the same bot (different chat ids, same token),
   there must be exactly ONE poller for that token, not one per channel
   (two independent pollers on the same token would race for the same
   offset and drop/duplicate updates). The supervisor reconciles the set of
   listener threads against the currently-configured tokens every few
   seconds — starting one for a newly-configured token, stopping one whose
   token is no longer used by any channel. Each listener thread is the one
   place in this whole codebase that reads INCOMING Telegram updates
   (`get_telegram_updates`); an approval `callback_query` (a button press) is
   verified against the set of ALL currently-configured chat ids (see
   TRUST MODEL below), then calls the EXACT SAME `approve_action_core`/
   `reject_action_core` (`dashboard/routes/actions.py`) the Dashboard's own
   HTML buttons use — no second implementation to drift — then edits the
   original message to show the outcome and answers the callback.

Both the notify scan and every listener thread read `settings` FRESH on
every iteration before touching the network — editing a channel's Bot
Token/Chat ID on the "Alert Telegram" page takes effect on the very next
iteration, no Dashboard restart required (this config lives IN this same
process, unlike the Worker/Watcher-side Telegram channels).

TRUST MODEL — read before configuring any channel's Chat ID: authorization
here is "did this button press come from ONE OF the currently-configured
channels' chat ids", the SAME identities already used to decide whether
ANY alert is delivered there at all. If a chat is a private 1:1 chat with
one trusted operator, approving from Telegram is exactly as strong as that
operator's own Telegram account. If it's a GROUP chat with several people,
EVERYONE in that group can approve/reject any RISKY action currently
pending — same trust boundary as sharing Dashboard login credentials, just
a different set of people. Configuring 3 channels means 3 chats (or fewer,
if some share a chat id) all carry this same power — there is no way to
make one channel "notify only". There is NO mapping anywhere in this
codebase from a Telegram account to a specific Dashboard user — the audit
trail records `actor="telegram:<username-or-id>"`, never a real Dashboard
username, when a decision comes from here.

Deliberately synchronous throughout (shared/telegram_client.py's blocking
httpx calls, unchanged) — every loop runs entirely off FastAPI's own
asyncio event loop, in its own dedicated thread, so a ~30s Telegram
long-poll response never stalls a single Dashboard HTTP request.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from config.settings import settings
from dashboard import telegram_chat
from dashboard.routes.actions import (
    ActionConflictError,
    ActionNotFoundError,
    ApprovalOutcome,
    approve_action_core,
    cancel_grace_action_core,
    _prepare_pool_application_choice,
    reject_action_core,
)
from shared import db
from shared.clusters import get_default_cluster_id
from shared.models import Action, ActionStatus, Cluster, Incident
from shared.telegram_client import (
    TelegramSendError,
    answer_telegram_callback,
    edit_telegram_message,
    get_telegram_updates,
    set_telegram_commands,
    send_telegram_message_with_keyboard,
)

logger = logging.getLogger(__name__)

APPROVE_CALLBACK_PREFIX = "approve:"
REJECT_CALLBACK_PREFIX = "reject:"
POOL_APP_CALLBACK_PREFIX = "poolapp:"
CANCEL_GRACE_CALLBACK_PREFIX = "cancelgrace:"
# Telegram holds the getUpdates HTTP connection open for up to this long
# waiting for a new update — this IS each listener thread's own pacing, no
# separate sleep needed between iterations while updates keep arriving.
_LONG_POLL_TIMEOUT_SECONDS = 30
# Backoff between iterations when idle/unconfigured/after an API error, and
# the supervisor's own reconcile cadence — deliberately short (a config
# change on the "Alert Telegram" page should take effect quickly) but not
# zero (must never busy-loop).
_IDLE_BACKOFF_SECONDS = 5
_MAX_DIAGNOSIS_CHARS = 240
_MAX_SOLUTION_CHARS = 240
_MAX_COMMAND_CHARS = 320

_ACTION_SOLUTION_LABELS = {
    "restart_osd_daemon": "Khởi động lại daemon OSD bị lỗi.",
    "resync_ntp": "Đồng bộ lại thời gian hệ thống bằng chrony.",
    "enable_mon_msgr2": "Bật giao thức Ceph Messenger v2 cho MON.",
    "crash_archive_all": "Lưu trữ các crash report đã được kiểm tra.",
    "pg_repair_force": "Kiểm tra PG và thực hiện repair thủ công sau khi xác minh dữ liệu.",
    "investigate_manually": "Điều tra thủ công; chưa có thao tác tự động đủ an toàn.",
    "remove_invalid_rgw_default_key": (
        "Gỡ khóa RGW mặc định sai khi Vault SSE-S3 đã được xác nhận, rồi restart tuần tự RGW."
    ),
}

# (channel_key, bot_token field, chat_id field, enabled field) on
# config.settings.Settings — the single source of truth for which 3
# channels exist and their field names, used by both the notify and listen
# sides below. `enabled` field (2026-08-07) is the separate per-channel
# on/off toggle on the Alert Telegram page — a channel with a saved token+
# chat id but flipped OFF must not receive Duyệt/Từ chối broadcasts either,
# same as it stops receiving plain notification alerts.
_CHANNELS = (
    ("backup", "telegram_backup_bot_token", "telegram_backup_chat_id", "telegram_backup_enabled"),
    ("incident", "telegram_incident_bot_token", "telegram_incident_chat_id", "telegram_incident_enabled"),
    ("node", "telegram_node_bot_token", "telegram_node_chat_id", "telegram_node_enabled"),
    ("rgw", "telegram_rgw_bot_token", "telegram_rgw_chat_id", "telegram_rgw_enabled"),
)


def _configured_channels() -> list[tuple[str, str, str]]:
    """[(channel_key, bot_token, chat_id), ...] for every channel that has
    BOTH fields filled in AND is enabled — reads `settings` fresh on every
    call, never cached, so a config change is picked up on the very next
    scan/reconcile without a Dashboard restart."""
    result = []
    for key, token_field, chat_field, enabled_field in _CHANNELS:
        bot_token = getattr(settings, token_field)
        chat_id = getattr(settings, chat_field)
        enabled = getattr(settings, enabled_field)
        if bot_token and chat_id and enabled:
            result.append((key, bot_token, chat_id))
    return result


def _known_chat_ids() -> set[str]:
    return {str(chat_id) for _, _, chat_id in _configured_channels()}


def has_configured_channel() -> bool:
    """True if at least one of Backup/Lỗi cụm/Phần cứng has both Bot Token +
    Chat ID set — i.e. Duyệt/Từ chối is reachable via Telegram right now
    (see this module's/docs/telegram-alerts.md's mục 6: approval is a
    capability of ANY configured channel, not a 4th toggle). Used by
    dashboard/routes/incidents.py as the BASELINE signal for whether the
    Dashboard's own "Chờ duyệt" card is needed — deliberately still scoped
    to just the 3 global channels (unchanged since before Phase 2), NOT
    per-cluster channels: `channels_for_incident()` below is what incidents.py
    additionally checks PER pending action to catch a non-default cluster
    with no channel of its own, which this global boolean alone can't see."""
    return bool(_configured_channels())


def _cluster_channel(cluster: Cluster) -> tuple[str, str, str] | None:
    """(channel_key, bot_token, chat_id) for `cluster`'s own Telegram
    channel (2026-08-10, multi-tenant remediation Phase 2), or None if it
    hasn't configured one (bot_token/chat_id blank) or has it disabled.
    `channel_key` is namespaced ("cluster:<id>") so it can never collide
    with the fixed 3 global keys ("backup"/"incident"/"node") inside
    `Action.telegram_message_ids`."""
    if cluster.telegram_bot_token and cluster.telegram_chat_id and cluster.telegram_enabled:
        return (f"cluster:{cluster.id}", cluster.telegram_bot_token, cluster.telegram_chat_id)
    return None


def channels_for_incident(incident: Incident | None, session) -> list[tuple[str, str, str]]:
    """The Telegram channel(s) legitimately covering `incident` (2026-08-10,
    multi-tenant remediation Phase 2) — narrows to just an OWN configured
    channel for a non-default cluster's Incident instead of broadcasting to
    (or trusting) the 3 global channels. Falls back to `_configured_
    channels()` (the existing 3-global broadcast-to-all behavior, unchanged)
    when `incident` is None, has no `cluster_id`, belongs to the DEFAULT
    cluster, or belongs to a cluster that hasn't configured its own channel.

    Used by BOTH the broadcast side (`_notify_pending_actions`) and the
    callback TRUST check (`_handle_callback_query`) so they can never drift
    apart — the set of chats a pending action is sent TO is always exactly
    the set allowed to approve/reject it."""
    if incident is not None and incident.cluster_id is not None:
        if incident.cluster_id != get_default_cluster_id(session):
            cluster = session.get(Cluster, incident.cluster_id)
            if cluster is not None:
                own_channel = _cluster_channel(cluster)
                return [own_channel] if own_channel is not None else []
    return _configured_channels()


def channels_for_action(
    action: Action | None, incident: Incident | None, session
) -> list[tuple[str, str, str]]:
    """Return only the approval channel(s) relevant to this action.

    A non-default cluster's own channel remains the strongest routing rule.
    For the default cluster, an Incident approval belongs only in the
    ``incident`` (Lỗi cụm) channel. The old broadcast-to-all fallback caused
    one Ceph warning (notably POOL_APP_NOT_ENABLED) to appear in Backup,
    Lỗi cụm and Phần cứng at the same time.
    """
    channels = channels_for_incident(incident, session)
    # A per-cluster channel is already a single namespaced destination;
    # never filter it by one of the three global channel keys.
    if incident is not None and incident.cluster_id is not None:
        default_cluster_id = get_default_cluster_id(session)
        if incident.cluster_id != default_cluster_id:
            return channels
    wanted = "rgw" if action is not None and action.action_id == "remove_invalid_rgw_default_key" else "incident"
    return [channel for channel in channels if channel[0] == wanted]


# 2026-08-10 (multi-tenant remediation Phase 2): `_listen_supervisor_loop`'s
# reconcile tick runs every `_IDLE_BACKOFF_SECONDS` (5s) for as long as the
# Dashboard process lives — querying the DB for cluster tokens that often,
# forever, adds needless steady-state load and was found (verified live in
# this session's own test suite) to occasionally starve OTHER background DB
# work of a connection/cursor in time. Cached at this coarser interval
# instead; a `_CLUSTER_TOKENS_CACHE_SECONDS`-stale cluster-channel pickup is
# an acceptable trade — a NEW/changed cluster channel already needs a
# Watcher restart to take effect for anything else about that cluster
# (dashboard/routes/clusters.py's own restart_watcher() call), which takes
# far longer than this cache's own staleness window.
_CLUSTER_TOKENS_CACHE_SECONDS = 30
_cluster_tokens_cache: tuple[float, set[str]] = (0.0, set())
_cluster_tokens_cache_lock = threading.Lock()


def _cluster_tokens_cached() -> set[str]:
    """Cluster-specific bot tokens, refreshed from the DB at most once every
    `_CLUSTER_TOKENS_CACHE_SECONDS` — see the module-level comment above for
    why. On a DB error, keeps serving the last known-good set (logged, never
    raised) rather than an empty one — a transient blip must not make every
    cluster's listener thread flap stopped/restarted."""
    global _cluster_tokens_cache
    with _cluster_tokens_cache_lock:
        last_fetched, cached = _cluster_tokens_cache
        now = time.monotonic()
        if now - last_fetched < _CLUSTER_TOKENS_CACHE_SECONDS:
            return cached
        tokens: set[str] = set()
        try:
            with db.SessionLocal() as session:
                for cluster in session.query(Cluster).filter(Cluster.is_active.is_(True)).all():
                    own_channel = _cluster_channel(cluster)
                    if own_channel is not None:
                        tokens.add(own_channel[1])
        except Exception:
            logger.exception("telegram_approval_bot: _cluster_tokens_cached failed to query clusters")
            return cached
        _cluster_tokens_cache = (now, tokens)
        return tokens


def _all_configured_tokens() -> set[str]:
    """Every bot token currently in use across the 3 global channels AND
    every active Cluster's own configured+enabled channel (2026-08-10,
    Phase 2) — the full set `_listen_supervisor_loop` must keep exactly one
    poller running per token for (see this module's own docstring on why
    grouping is by token, not by channel). `_configured_channels()` is a
    pure `settings` read (never fails, always fresh); the cluster half is
    `_cluster_tokens_cached()` above."""
    tokens = {token for _, token, _ in _configured_channels()} | _cluster_tokens_cached()
    chatbox_token = telegram_chat.configured_token()
    if chatbox_token:
        tokens.add(chatbox_token)
    return tokens


def _compact_text(value: str | None, limit: int) -> str:
    """Turn verbose model/command output into one phone-friendly line."""
    compact = " ".join((value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _action_message_text(action: Action, incident: Incident | None, session) -> str:
    from shared import change_risk

    # `attach_summary` persists the change-risk result by appending it to the
    # Action rationale. Keep the original operator/AI recommendation for the
    # phone-facing "Giải pháp đề xuất" line; an internal risk summary is
    # useful for audit but is not a replacement for what the action does.
    original_rationale = action.rationale
    risk = change_risk.assess_and_record(session, action=action, incident=incident)
    change_risk.attach_summary(action, risk)
    session.flush()
    lines = []
    # 2026-08-07: same cluster-name prefix as shared/telegram_alerts.py's
    # _with_cluster_prefix — this module runs in the Dashboard process (not
    # Watcher/Worker) and builds its own message text, so it needs its own
    # copy rather than importing across that layering boundary.
    #
    # 2026-08-10 (multi-tenant remediation Phase 1): looks up the Incident's
    # OWN cluster by cluster_id instead of always using the global
    # settings.cluster_name — an operator approving from Telegram must be
    # able to tell WHICH cluster a RISKY command is about to run against
    # when more than one is configured, not just the default one's label
    # (or nothing at all, if settings.cluster_name was left blank).
    # cluster_id=None still means the default cluster, same convention as
    # Incident.cluster_id everywhere else — falls back to settings.
    # cluster_name exactly as before for that case.
    cluster_name = ""
    if incident is not None and incident.cluster_id is not None:
        cluster = session.get(Cluster, incident.cluster_id)
        if cluster is not None:
            cluster_name = cluster.name.strip()
    else:
        cluster_name = settings.cluster_name.strip()
    if cluster_name:
        lines.append(f"\U0001f4cd Cụm: {cluster_name}")
    lines.append(f"📋 Chờ duyệt: {action.action_id}")
    # incident.diagnosis_text is the router's plain-language root-cause
    # explanation (worker/llm/router_client.py::diagnose_incident) — the
    # SAME field the Dashboard's own "Chờ duyệt" card prefers over
    # action.rationale (dashboard/templates/index.html). Telegram used to
    # only ever send `rationale` (a short "why this action_id" note) and
    # never the diagnosis itself — the actual "giải pháp" an operator asked
    # about lived only on the Dashboard. Show both when they differ.
    diagnosis = _compact_text(incident.diagnosis_text if incident else None, _MAX_DIAGNOSIS_CHARS)
    if diagnosis:
        lines.append(f"⚠️ Chẩn đoán: {diagnosis}")
    solution = _compact_text(original_rationale, _MAX_SOLUTION_CHARS)
    if not solution:
        solution = _ACTION_SOLUTION_LABELS.get(
            action.action_id, f"Thực hiện hành động {action.action_id}."
        )
    lines.append(f"🔧 Giải pháp đề xuất: {solution}")
    if _needs_pool_application_choice(action, incident):
        pool_params = _pool_application_params(action, incident)
        if pool_params.get("pool_name") and not pool_params.get("app_name"):
            lines.append(f"🏊 Pool: {pool_params['pool_name']} — hãy chọn application bên dưới")
    if action.proposed_command:
        lines.append(f"💻 Lệnh: {_compact_text(action.proposed_command, _MAX_COMMAND_CHARS)}")
    lines.append(f"🆔 {action.id[:8]}")
    if action.status == ActionStatus.GRACE_PENDING.value and action.grace_until is not None:
        remaining = max(0, int((action.grace_until - datetime.utcnow()).total_seconds()))
        lines.append(f"⏳ Autopilot lab sẽ chạy sau khoảng {remaining} giây nếu không bị hủy.")
    return "\n".join(lines)


def _keyboard_for(action_id: str) -> list[tuple[str, str]]:
    return [
        ("✅ Duyệt", f"{APPROVE_CALLBACK_PREFIX}{action_id}"),
        ("❌ Từ chối", f"{REJECT_CALLBACK_PREFIX}{action_id}"),
    ]


def _pool_application_params(action: Action, incident: Incident | None) -> dict:
    """Return choice parameters for both new and legacy pool warnings."""
    try:
        params = json.loads(action.action_params or "{}")
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}
    if not params.get("pool_name") and incident is not None and incident.ceph_code == "POOL_APP_NOT_ENABLED":
        evidence = " ".join(filter(None, (incident.diagnosis_text, action.rationale, incident.log_excerpt)))
        match = re.search(r"pool\s+['\"]([^'\"]+)['\"]", evidence, re.IGNORECASE)
        if match:
            params["pool_name"] = match.group(1)
    return params


def _needs_pool_application_choice(action: Action, incident: Incident | None) -> bool:
    return (
        action.action_id == "enable_pool_application"
        or (action.action_id == "investigate_manually" and incident is not None
            and incident.ceph_code == "POOL_APP_NOT_ENABLED")
    )


def _approval_keyboard(action: Action, incident: Incident | None = None) -> list[tuple[str, str]]:
    if action.status == ActionStatus.GRACE_PENDING.value:
        return [("🛑 Hủy Autopilot", f"{CANCEL_GRACE_CALLBACK_PREFIX}{action.id}")]
    if _needs_pool_application_choice(action, incident):
        params = _pool_application_params(action, incident)
        if params.get("pool_name") and not params.get("app_name"):
            return [
                ("💾 RBD", f"{POOL_APP_CALLBACK_PREFIX}rbd:{action.id}"),
                ("📁 CephFS", f"{POOL_APP_CALLBACK_PREFIX}cephfs:{action.id}"),
                ("🌐 RGW", f"{POOL_APP_CALLBACK_PREFIX}rgw:{action.id}"),
                ("❌ Từ chối", f"{REJECT_CALLBACK_PREFIX}{action.id}"),
            ]
    return _keyboard_for(action.id)


def _load_message_ids(action: Action) -> dict:
    if not action.telegram_message_ids:
        return {}
    try:
        parsed = json.loads(action.telegram_message_ids)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _notify_pending_actions() -> None:
    """One scan cycle. Reads the candidate id list in one query, then
    re-checks + broadcasts-to-missing-channels + stamps EACH Action in its
    own session/transaction — a failure on one candidate (or one channel of
    one candidate) must not lose progress already made on the others in
    this same scan, and a status change that happened between the initial
    query and this row's turn (approved/rejected via the Dashboard in the
    meantime) must be re-checked fresh, not assumed still true from the
    initial query.

    Deliberately queries ALL PENDING_APPROVAL rows every cycle (no
    `telegram_notified_at IS NULL` SQL filter like the single-channel
    design used) — "already sent to every currently-configured channel" is
    now a JSON-membership check, not expressible as a simple indexed
    filter, and this tool's expected scale (a handful of concurrently
    pending actions on a lab/small deployment) makes that an acceptable
    trade for the simpler, more correct broadcast/backfill semantics.

    2026-08-10 (multi-tenant remediation Phase 2): no longer bails out early
    just because the 3 GLOBAL channels are unconfigured — a non-default
    cluster's own channel (`channels_for_incident`, resolved per action
    below) may still need this Action delivered even when none of the 3
    global ones are set up at all."""
    with db.SessionLocal() as session:
        candidate_ids = [
            row.id
            for row in session.query(Action.id).filter(Action.status.in_([
                ActionStatus.PENDING_APPROVAL.value, ActionStatus.GRACE_PENDING.value,
            ])).all()
        ]

    for action_id in candidate_ids:
        with db.SessionLocal() as session:
            action = session.get(Action, action_id)
            if action is None or action.status not in {
                ActionStatus.PENDING_APPROVAL.value, ActionStatus.GRACE_PENDING.value,
            }:
                continue
            incident = session.get(Incident, action.incident_id)
            # Action-aware routing prevents a storage benchmark approval
            # from leaking into unrelated Backup and Hardware channels.
            target_channels = channels_for_action(action, incident, session)
            if not target_channels:
                continue
            sent = _load_message_ids(action)
            # Actions created before pool-application choices were supported
            # already have a generic Telegram message id. Re-broadcast those
            # exactly once with the new three-choice keyboard.
            pool_params = _pool_application_params(action, incident)
            is_legacy_pool_choice = (
                action.action_id == "investigate_manually"
                and _needs_pool_application_choice(action, incident)
                and bool(pool_params.get("pool_name"))
            )
            if is_legacy_pool_choice and not pool_params.get("telegram_pool_choices_sent"):
                sent = {}
                pool_params["telegram_pool_choices_sent"] = True
                action.action_params = json.dumps(pool_params)
            missing = [ch for ch in target_channels if ch[0] not in sent]
            if not missing:
                continue

            message_text = _action_message_text(action, incident, session)
            changed = False
            for channel_key, bot_token, chat_id in missing:
                try:
                    message_id = send_telegram_message_with_keyboard(
                        bot_token,
                        chat_id,
                        message_text,
                        _approval_keyboard(action, incident),
                    )
                except TelegramSendError:
                    logger.exception(
                        "telegram_approval_bot: failed to send approval request for action %s on channel %s",
                        action_id,
                        channel_key,
                    )
                    continue
                sent[channel_key] = message_id
                changed = True

            if changed:
                action.telegram_message_ids = json.dumps(sent)
                action.telegram_notified_at = datetime.utcnow()
                session.commit()


def _notify_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _notify_pending_actions()
        except Exception:
            logger.exception("telegram_approval_bot: _notify_pending_actions crashed")
        stop_event.wait(max(1, settings.telegram_approval_scan_interval_seconds))


_OUTCOME_EDIT_SUFFIX = {
    ApprovalOutcome.APPROVED: "\n\n✅ ĐÃ DUYỆT.",
    ApprovalOutcome.ACKNOWLEDGED: "\n\n✅ Đã xác nhận (không có lệnh tự động để chạy cho mục này).",
    ApprovalOutcome.REJECTED: "\n\n❌ ĐÃ TỪ CHỐI.",
    ApprovalOutcome.ALREADY_HANDLED: "\n\n⚠️ Đã được xử lý từ trước (có thể qua Dashboard hoặc kênh khác).",
    # AI roadmap Pha 0.4 (section 3.3, stale-evidence check).
    ApprovalOutcome.EXPIRED: "\n\n⌛ Đề xuất đã hết hạn — vào Dashboard để Từ chối và chờ chẩn đoán lại.",
}
_OUTCOME_TOAST = {
    ApprovalOutcome.APPROVED: "Đã duyệt",
    ApprovalOutcome.ACKNOWLEDGED: "Đã xác nhận",
    ApprovalOutcome.REJECTED: "Đã từ chối",
    ApprovalOutcome.ALREADY_HANDLED: "Đã được xử lý từ trước",
    ApprovalOutcome.EXPIRED: "Đề xuất đã hết hạn",
}


def _actor_for(callback_query: dict) -> str:
    sender = callback_query.get("from") or {}
    username = sender.get("username")
    user_id = sender.get("id")
    return f"telegram:{username or user_id or 'unknown'}"


def _handle_callback_query(callback_query: dict, bot_token: str) -> None:
    """`bot_token` is the token of the listener thread that received this
    update — answering the callback / editing the message MUST use that
    same bot (a different configured bot cannot act on a message it never
    sent)."""
    data = callback_query.get("data") or ""
    callback_id = callback_query.get("id")
    message = callback_query.get("message") or {}
    message_id = message.get("message_id")
    incoming_chat_id = str((message.get("chat") or {}).get("id", ""))

    selected_pool_app = None
    if data.startswith(POOL_APP_CALLBACK_PREFIX):
        remainder = data[len(POOL_APP_CALLBACK_PREFIX):]
        selected_pool_app, separator, action_id = remainder.partition(":")
        if not separator or selected_pool_app not in {"rbd", "cephfs", "rgw"}:
            logger.warning("telegram_approval_bot: invalid pool application callback=%r", data)
            return
        core_fn = approve_action_core
    elif data.startswith(APPROVE_CALLBACK_PREFIX):
        action_id = data[len(APPROVE_CALLBACK_PREFIX):]
        core_fn = approve_action_core
    elif data.startswith(REJECT_CALLBACK_PREFIX):
        action_id = data[len(REJECT_CALLBACK_PREFIX):]
        core_fn = reject_action_core
    elif data.startswith(CANCEL_GRACE_CALLBACK_PREFIX):
        action_id = data[len(CANCEL_GRACE_CALLBACK_PREFIX):]
        core_fn = cancel_grace_action_core
    else:
        logger.warning("telegram_approval_bot: unrecognized callback_data=%r", data)
        return

    # TRUST MODEL (per-cluster scoped, 2026-08-10) — see this module's own
    # docstring: resolve the chat ids LEGITIMATELY covering THIS SPECIFIC
    # action's cluster via `channels_for_incident`. Global configured chats
    # remain trusted for callbacks (including old messages sent before
    # category routing was narrowed), while a chat configured for a DIFFERENT cluster's own
    # channel (or one of the 3 global channels, when this action's cluster
    # has its own) is no longer trusted just for being "some configured
    # channel somewhere" — unlike before Phase 2. An action_id that no
    # longer resolves to a real Action/Incident falls back to the 3 global
    # channels' chat ids (safe: `core_fn` below still raises
    # ActionNotFoundError right after, handled identically to before).
    with db.SessionLocal() as session:
        action_for_trust = session.get(Action, action_id)
        incident_for_trust = (
            session.get(Incident, action_for_trust.incident_id) if action_for_trust is not None else None
        )
        legit_chat_ids = {
            str(chat_id)
            for _, _, chat_id in channels_for_incident(incident_for_trust, session)
        }

    if not legit_chat_ids or incoming_chat_id not in legit_chat_ids:
        logger.warning(
            "telegram_approval_bot: ignoring callback_query from unauthorized chat_id=%s for action_id=%s",
            incoming_chat_id,
            action_id,
        )
        if callback_id:
            try:
                answer_telegram_callback(bot_token, callback_id, "Không có quyền")
            except TelegramSendError:
                pass
        return

    actor = _actor_for(callback_query)
    try:
        if selected_pool_app is not None:
            with db.SessionLocal() as session:
                choice_action = session.get(Action, action_id)
                if choice_action is None:
                    raise ActionNotFoundError(action_id)
                choice_incident = session.get(Incident, choice_action.incident_id)
                is_legacy_choice = (
                    choice_action.action_id == "investigate_manually"
                    and choice_incident is not None
                    and choice_incident.ceph_code == "POOL_APP_NOT_ENABLED"
                )
                if not is_legacy_choice:
                    try:
                        params = json.loads(choice_action.action_params or "{}")
                    except ValueError:
                        params = {}
                    if not params.get("pool_name"):
                        raise ActionConflictError("Không xác định được tên pool")
                    params["app_name"] = selected_pool_app
                    choice_action.action_params = json.dumps(params)
                    session.commit()
            if is_legacy_choice:
                _prepare_pool_application_choice(action_id, selected_pool_app)
        result = core_fn(action_id, actor)
        edit_suffix = _OUTCOME_EDIT_SUFFIX[result.outcome]
        toast = _OUTCOME_TOAST[result.outcome]
        if data.startswith(CANCEL_GRACE_CALLBACK_PREFIX) and result.outcome == ApprovalOutcome.REJECTED:
            edit_suffix = "\n\n🛑 ĐÃ HỦY AUTOPILOT TRONG GRACE PERIOD."
            toast = "Đã hủy Autopilot"
    except ActionNotFoundError:
        edit_suffix = "\n\n⚠️ Không tìm thấy đề xuất này (có thể đã bị xoá)."
        toast = "Không tìm thấy đề xuất này"
    except ActionConflictError as exc:
        edit_suffix = f"\n\n⚠️ {exc.detail}"
        toast = exc.detail

    if callback_id:
        try:
            answer_telegram_callback(bot_token, callback_id, toast)
        except TelegramSendError:
            logger.exception("telegram_approval_bot: answerCallbackQuery failed")

    if message_id is not None:
        with db.SessionLocal() as session:
            action = session.get(Action, action_id)
            if action is not None:
                incident = session.get(Incident, action.incident_id)
                base_text = _action_message_text(action, incident, session)
            else:
                base_text = f"Action ID: {action_id}"
        try:
            edit_telegram_message(bot_token, incoming_chat_id, message_id, base_text + edit_suffix)
        except TelegramSendError:
            logger.exception("telegram_approval_bot: failed to edit message %s after decision", message_id)


_UPDATE_OFFSET_FILE = Path("/var/lib/ceph-ai/telegram-update-offsets.json")
_update_offset_lock = threading.Lock()


def _offset_key(bot_token: str) -> str:
    """A stable key without putting a Bot Token on disk or in logs."""
    return hashlib.sha256(bot_token.encode()).hexdigest()


def _load_update_offset(bot_token: str) -> int | None:
    with _update_offset_lock:
        try:
            payload = json.loads(_UPDATE_OFFSET_FILE.read_text())
            value = payload.get(_offset_key(bot_token)) if isinstance(payload, dict) else None
            return int(value) if isinstance(value, int) and value >= 0 else None
        except FileNotFoundError:
            return None
        except Exception:
            logger.exception("telegram_approval_bot: failed to load persisted update offsets")
            return None


def _save_update_offset(bot_token: str, offset: int) -> None:
    """Durably acknowledge an update after its handler has run.

    Telegram redelivers updates until a later offset is supplied. Persisting
    this cursor eliminates duplicate AI turns after a Dashboard restart while
    retaining at-least-once delivery if the process dies during a handler.
    """
    if offset < 0:
        return
    with _update_offset_lock:
        try:
            payload: dict[str, int] = {}
            try:
                loaded = json.loads(_UPDATE_OFFSET_FILE.read_text())
                if isinstance(loaded, dict):
                    payload = {str(key): value for key, value in loaded.items() if isinstance(value, int) and value >= 0}
            except FileNotFoundError:
                pass
            _UPDATE_OFFSET_FILE.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            payload[_offset_key(bot_token)] = offset
            temporary = _UPDATE_OFFSET_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, sort_keys=True))
            os.chmod(temporary, 0o600)
            os.replace(temporary, _UPDATE_OFFSET_FILE)
        except Exception:
            logger.exception("telegram_approval_bot: failed to persist update offset")


def _listen_loop_for_token(bot_token: str, stop_event: threading.Event) -> None:
    """One long-polling loop for a single bot token — see this module's own
    docstring for why this is grouped by TOKEN rather than by channel."""
    offset = _load_update_offset(bot_token)
    if bot_token == telegram_chat.configured_token():
        try:
            set_telegram_commands(
                bot_token,
                [
                    {"command": "start", "description": "Mở Chatbox AI"},
                    {"command": "model", "description": "Chọn chế độ AI"},
                    {"command": "cluster", "description": "Chọn cụm Ceph"},
                    {"command": "single", "description": "Chế độ 1 AI"},
                    {"command": "dual", "description": "Hai AI sửa trong workspace cô lập"},
                    {"command": "single_full", "description": "1 AI toàn quyền (cần cấp riêng)"},
                    {"command": "stop", "description": "Dừng phiên AI đang chạy"},
                    {"command": "status", "description": "Xem trạng thái phiên AI"},
                    {"command": "ask", "description": "Gửi câu hỏi cho AI"},
                    {"command": "new", "description": "Bắt đầu chat mới"},
                    {"command": "help", "description": "Xem trợ giúp"},
                ],
            )
        except TelegramSendError:
            logger.exception("telegram_approval_bot: failed to register Chatbox slash commands")
        try:
            telegram_chat.report_interrupted_full_runs(bot_token)
        except Exception:
            logger.exception("telegram_approval_bot: failed to report interrupted Single Full run")
    while not stop_event.is_set():
        try:
            updates = get_telegram_updates(
                bot_token,
                offset,
                _LONG_POLL_TIMEOUT_SECONDS,
                allowed_updates=["callback_query", "message"],
            )
        except TelegramSendError:
            logger.exception(
                "telegram_approval_bot: getUpdates failed for bot token suffix=%s",
                bot_token[-6:],
            )
            stop_event.wait(_IDLE_BACKOFF_SECONDS)
            continue

        for update in updates:
            update_id = update.get("update_id")
            next_offset = (int(update_id) + 1) if isinstance(update_id, int) else None
            callback_query = update.get("callback_query")
            if callback_query:
                callback_data = str(callback_query.get("data") or "")
                if callback_data.startswith((
                    telegram_chat.CHAT_CONFIRM_PREFIX,
                    telegram_chat.CHAT_APPROVE_PREFIX,
                    telegram_chat.DUAL_STOP_PREFIX,
                    telegram_chat.QUOTA_LOGIN_PREFIX,
                    telegram_chat.AI_MODE_PREFIX,
                    telegram_chat.CLUSTER_SELECT_PREFIX,
                )):
                    try:
                        result = telegram_chat.run_callback_sync(callback_query, bot_token)
                        if result is not None:
                            callback_id = callback_query.get("id")
                            if callback_id:
                                answer_telegram_callback(bot_token, callback_id, result)
                    except Exception:
                        logger.exception("telegram_approval_bot: unexpected error handling chat callback")
                else:
                    try:
                        _handle_callback_query(callback_query, bot_token)
                    except Exception:
                        logger.exception("telegram_approval_bot: unexpected error handling callback_query")
            message = update.get("message")
            if message:
                try:
                    if not telegram_chat.handle_stop_message(message, bot_token):
                        if not telegram_chat.handle_status_message(message, bot_token):
                            telegram_chat.enqueue_message(message, bot_token)
                except Exception:
                    logger.exception("telegram_approval_bot: unexpected error handling chat message")
            if next_offset is not None and next_offset > (offset or 0):
                offset = next_offset
                _save_update_offset(bot_token, offset)


def _listen_supervisor_loop(stop_event: threading.Event) -> None:
    """Reconciles the set of running `_listen_loop_for_token` threads
    against the currently-configured bot tokens every `_IDLE_BACKOFF_SECONDS`
    — starts one for a newly-configured token, stops one whose token no
    channel uses anymore. A stopped thread exits after its current
    long-poll call returns (up to ~30s later), same "applies on the next
    iteration" latency this module has always had for config changes.

    DEBOUNCED on purpose: a token only gets a listener thread once it has
    been seen configured on TWO CONSECUTIVE reconcile passes (i.e. it held
    steady across at least one full `_IDLE_BACKOFF_SECONDS` interval), not
    on the very first sighting. A genuinely-saved channel (via the "Alert
    Telegram" page or `.env`) trivially satisfies this — it's still
    configured 5s later — costing it nothing but one extra poll interval of
    startup latency. A token that only exists for the sub-millisecond
    duration of a single test's monkeypatch/direct-`settings`-mutation
    (this module has no other way to distinguish "real" from "test" state)
    is essentially never observed on two separate 5s-apart polls, so it
    never spawns a real, network-calling thread. Without this, a spurious
    thread each making a real, ~30-40s-blocking `getUpdates` HTTP call to
    Telegram was found (2026-08-06) to pile up across a full test-suite run
    and starve unrelated tests of threads/connections — the whole reason
    this debounce exists."""
    listeners: dict[str, tuple[threading.Thread, threading.Event]] = {}
    previously_seen_tokens: set[str] = set()
    while not stop_event.is_set():
        current_tokens = _all_configured_tokens()
        stable_tokens = current_tokens & previously_seen_tokens
        previously_seen_tokens = current_tokens

        for token in stable_tokens - listeners.keys():
            token_stop = threading.Event()
            thread = threading.Thread(target=_listen_loop_for_token, args=(token, token_stop), daemon=True)
            thread.start()
            listeners[token] = (thread, token_stop)

        for token in list(listeners.keys() - current_tokens):
            _, token_stop = listeners.pop(token)
            token_stop.set()

        stop_event.wait(_IDLE_BACKOFF_SECONDS)

    for _, token_stop in listeners.values():
        token_stop.set()


_threads: list[threading.Thread] = []
_stop_event = threading.Event()
_listener_lock_file = None


def _runtime_dir() -> Path:
    return Path(os.environ.get("CEPH_AI_RUNTIME_DIR", "/tmp/ceph-ai"))


def _acquire_listener_lock() -> bool:
    """Guarantee one Telegram polling owner per host/runtime directory."""
    global _listener_lock_file
    if _listener_lock_file is not None:
        return True
    lock_path = _runtime_dir() / "telegram-listener.lock"
    handle = None
    try:
        lock_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if handle is not None:
            handle.close()
        logger.warning(
            "telegram_approval_bot: another local process already owns Telegram getUpdates; skipping listener startup"
        )
        return False
    except Exception:
        if handle is not None:
            handle.close()
        logger.exception("telegram_approval_bot: failed to acquire Telegram listener lock")
        return False
    _listener_lock_file = handle
    return True


def start() -> None:
    """Called once from `dashboard/app.py`'s lifespan startup. Idempotent
    — a second call (e.g. FastAPI's TestClient re-entering the app's
    lifespan across many tests within one process, or an app reload) is a
    no-op; both top-level threads (and every dynamically-spawned per-token
    listener) are daemon=True so they never block process exit and need no
    explicit shutdown hook."""
    if _threads:
        return
    if not _acquire_listener_lock():
        return
    for target in (_notify_loop, _listen_supervisor_loop):
        thread = threading.Thread(target=target, args=(_stop_event,), daemon=True)
        thread.start()
        _threads.append(thread)
