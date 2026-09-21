"""Trang "Alert Telegram" — 2026-08-06: dời hẳn khỏi Settings, tách 3 kênh
Telegram độc lập (Backup/Lỗi cụm/Phần cứng), mỗi kênh có Bot Token + Chat ID
riêng, lưu qua route riêng của chính kênh đó. Không còn 1 form dùng chung
cho cả 4 mục như trước — xem docs/telegram-alerts.md để biết đầy đủ thiết
kế (broadcast Duyệt/Từ chối tới mọi kênh đã cấu hình, gom listener theo bot
token trong dashboard/telegram_approval_bot.py).

Toàn trang admin-only, cùng mức quyền "Cảnh báo Telegram" từng có trên
Settings trước đây (trang chứa Bot Token — bí mật).
"""

import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.settings import _mask_key, _require_admin_privilege, restart_watcher, restart_worker
from dashboard.templating import make_templates
from shared import db, env_config, telegram_outbox
from shared.models import TelegramChannelConfigChange, TelegramChannelLayout, TelegramManagedChannel
from shared.telegram_client import TelegramSendError, send_telegram_message

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# 2026-08-07: settings.cluster_name's own .env var name — a single field
# shared by all 3 channels (see config/settings.py's docstring on that
# field), so it gets its own constant here rather than an entry in one of
# the per-channel env_names dicts above. Same "local module-level constant"
# posture as ROUTER_API_KEY_ENV_NAME in dashboard/routes/settings.py, not
# added to shared/env_config.py — nothing outside this route needs it.
CLUSTER_NAME_ENV_NAME = "CLUSTER_NAME"

# Single source of truth for the channels this page manages — label,
# which config.settings.Settings fields back it, which .env variable names
# shared/env_config.py maps those fields to, and which process actually
# reads it (so a save here restarts ONLY that process, not both like the
# old shared-config design always did).
_CHANNELS: dict[str, dict] = {
    "backup": {
        "label": "Cảnh báo Backup",
        "bot_token_field": "telegram_backup_bot_token",
        "chat_id_field": "telegram_backup_chat_id",
        "enabled_field": "telegram_backup_enabled",
        "env_names": env_config.TELEGRAM_BACKUP_ENV_NAMES,
        "restart": "worker",
        "approval_enabled": True,
    },
    "incident": {
        "label": "Cảnh báo lỗi cụm",
        "bot_token_field": "telegram_incident_bot_token",
        "chat_id_field": "telegram_incident_chat_id",
        "enabled_field": "telegram_incident_enabled",
        "env_names": env_config.TELEGRAM_INCIDENT_ENV_NAMES,
        "restart": "watcher",
        "approval_enabled": True,
    },
    "rbd-forecast": {
        "label": "Dự báo sớm RBD Volume",
        "bot_token_field": "telegram_rbd_forecast_bot_token",
        "chat_id_field": "telegram_rbd_forecast_chat_id",
        "enabled_field": "telegram_rbd_forecast_enabled",
        "env_names": env_config.TELEGRAM_RBD_FORECAST_ENV_NAMES,
        "restart": "watcher",
        "approval_enabled": False,
    },
    "node": {
        "label": "Cảnh báo phần cứng",
        "bot_token_field": "telegram_node_bot_token",
        "chat_id_field": "telegram_node_chat_id",
        "enabled_field": "telegram_node_enabled",
        "env_names": env_config.TELEGRAM_NODE_ENV_NAMES,
        "restart": "watcher",
        "approval_enabled": True,
    },
    "code-repair": {
        "label": "AI Code Repair — sửa hệ thống",
        "bot_token_field": "telegram_code_repair_bot_token",
        "chat_id_field": "telegram_code_repair_chat_id",
        "enabled_field": "telegram_code_repair_enabled",
        "env_names": env_config.TELEGRAM_CODE_REPAIR_ENV_NAMES,
        "restart": "none",
        "approval_enabled": False,
    },
    "rgw": {
        "label": "Cảnh báo RGW — AI phân tích",
        "bot_token_field": "telegram_rgw_bot_token",
        "chat_id_field": "telegram_rgw_chat_id",
        "enabled_field": "telegram_rgw_enabled",
        "env_names": env_config.TELEGRAM_RGW_ENV_NAMES,
        "restart": "watcher",
        "approval_enabled": False,
    },
    "vault": {
        "label": "Cảnh báo Vault — bảo mật",
        "bot_token_field": "telegram_vault_bot_token",
        "chat_id_field": "telegram_vault_chat_id",
        "enabled_field": "telegram_vault_enabled",
        "env_names": env_config.TELEGRAM_VAULT_ENV_NAMES,
        "restart": "none",
        "approval_enabled": False,
    },
    "chatbox-ai": {
        "label": "Chatbox AI — Telegram hai chiều",
        "bot_token_field": "telegram_chatbox_bot_token",
        "chat_id_field": "telegram_chatbox_chat_id",
        "enabled_field": "telegram_chatbox_enabled",
        "env_names": env_config.TELEGRAM_CHATBOX_ENV_NAMES,
        "restart": "worker",
        "approval_enabled": False,
        "chatbox": True,
    },
}

_PERFORMANCE_RCA_KEY = "performance-rca"
_BUILTIN_CHANNEL_ORDER = [*list(_CHANNELS), _PERFORMANCE_RCA_KEY]
_LAYOUT_ID = "default"


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "").lower()


def _channel_or_404(channel: str) -> dict:
    info = _CHANNELS.get(channel)
    if info is None:
        raise HTTPException(status_code=404, detail="Kênh Telegram không hợp lệ")
    return info


def _parse_layout_value(raw: str | None, fallback):
    try:
        value = json.loads(raw or "")
        return value if isinstance(value, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def _layout_state(session) -> tuple[list[str], set[str], dict[str, str]]:
    row = session.get(TelegramChannelLayout, _LAYOUT_ID)
    if row is None:
        return list(_BUILTIN_CHANNEL_ORDER), set(), {}
    return (
        _parse_layout_value(row.order_json, []),
        set(_parse_layout_value(row.hidden_json, [])),
        _parse_layout_value(row.display_names_json, {}),
    )


def _save_layout(
    session,
    *,
    order: list[str] | None = None,
    hidden: set[str] | None = None,
    display_names: dict[str, str] | None = None,
    actor: str = "",
) -> None:
    row = session.get(TelegramChannelLayout, _LAYOUT_ID)
    if row is None:
        row = TelegramChannelLayout(id=_LAYOUT_ID)
        session.add(row)
    current_order, current_hidden, current_names = _layout_state(session)
    row.order_json = json.dumps(order if order is not None else current_order, ensure_ascii=False)
    row.hidden_json = json.dumps(sorted(hidden if hidden is not None else current_hidden), ensure_ascii=False)
    row.display_names_json = json.dumps(display_names if display_names is not None else current_names, ensure_ascii=False)
    row.updated_by = actor


def _channel_key_for_managed(row: TelegramManagedChannel) -> str:
    return f"custom:{row.id}"


def _managed_channel_by_key(session, key: str) -> TelegramManagedChannel | None:
    if not key.startswith("custom:"):
        return None
    return session.get(TelegramManagedChannel, key.removeprefix("custom:"))


def _public_managed_channel(row: TelegramManagedChannel) -> dict:
    configured = bool(row.bot_token and row.chat_id)
    return {
        "key": _channel_key_for_managed(row),
        "id": row.id,
        "label": row.name,
        "chat_id": row.chat_id,
        "enabled": row.enabled,
        "status_label": "Đang bật" if configured and row.enabled else "Đã tắt" if configured else "Chưa cấu hình",
        "masked_bot_token": _mask_key(row.bot_token) if row.bot_token else None,
        "custom": True,
        "approval_enabled": False,
    }


def _fixed_channel_info(key: str) -> dict:
    if key == _PERFORMANCE_RCA_KEY:
        return {
            "key": key,
            "label": "Cảnh báo Performance RCA",
            "enabled": settings.telegram_performance_rca_enabled,
            "chat_id": settings.telegram_incident_chat_id,
            "masked_bot_token": _mask_key(settings.telegram_incident_bot_token) if settings.telegram_incident_bot_token else None,
            "status_label": "Đang bật" if settings.telegram_performance_rca_enabled else "Đã tắt",
            "custom": False,
            "rca": True,
            "approval_enabled": False,
        }
    info = _CHANNELS[key]
    bot_token = getattr(settings, info["bot_token_field"])
    chat_id = getattr(settings, info["chat_id_field"])
    enabled = getattr(settings, info["enabled_field"])
    return {
        "key": key,
        "label": info["label"],
        "chat_id": chat_id,
        "enabled": enabled,
        "approval_enabled": info["approval_enabled"],
        "status_label": "Chưa cấu hình" if not bot_token or not chat_id else "Đang bật" if enabled else "Đã tắt",
        "masked_bot_token": _mask_key(bot_token) if bot_token else None,
        "custom": False,
        "rca": False,
    }


def _restart_label_for_channel(key: str) -> str:
    if key == _PERFORMANCE_RCA_KEY:
        return "Watcher"
    return {"worker": "Worker", "watcher": "Watcher"}.get(_CHANNELS[key]["restart"], "không cần restart dịch vụ")


async def _restart_channel_process(key: str) -> None:
    if key == _PERFORMANCE_RCA_KEY or _CHANNELS.get(key, {}).get("restart") == "watcher":
        await asyncio.to_thread(restart_watcher)
    elif _CHANNELS.get(key, {}).get("restart") == "worker":
        await asyncio.to_thread(restart_worker)


# Most-recent-first entries shown per channel on the page — this is an
# at-a-glance history, not a full export/search screen, so an unbounded
# query isn't needed.
_HISTORY_ROWS_PER_CHANNEL = 10


def _record_channel_config_change(channel: str, chat_id: str, bot_token: str, actor: str) -> None:
    """Called once per successful Bot Token/Chat ID save
    (`telegram_channel_submit` below) — never on Bật/Tắt (that endpoint
    doesn't touch either value). Best-effort: a history-row failure must
    never roll back or mask the config save that already succeeded."""
    try:
        with db.SessionLocal() as session:
            session.add(
                TelegramChannelConfigChange(
                    channel=channel,
                    chat_id=chat_id,
                    bot_token_masked=_mask_key(bot_token) if bot_token else "",
                    actor=actor,
                )
            )
            session.commit()
    except Exception:
        logger.exception("telegram_alerts: failed to record config-change history for channel %s", channel)


def _channel_history() -> dict[str, list[TelegramChannelConfigChange]]:
    """{channel_key: [most-recent-first rows]} for every channel — admin-only
    (this whole page requires `_require_admin_privilege`), used to answer
    "kênh này ai từng cấu hình, đổi lúc nào" on `/telegram-alerts/{channel}`."""
    result: dict[str, list[TelegramChannelConfigChange]] = {key: [] for key in _CHANNELS}
    with db.SessionLocal() as session:
        # Query each known channel independently so the database never loads
        # the full history table just to render ten rows per channel.
        for channel in _CHANNELS:
            result[channel] = (
                session.query(TelegramChannelConfigChange)
                .filter(TelegramChannelConfigChange.channel == channel)
                .order_by(TelegramChannelConfigChange.created_at.desc())
                .limit(_HISTORY_ROWS_PER_CHANNEL)
                .all()
            )
    return result


def _context(
    user: str,
    *,
    errors: dict[str, str] | None = None,
    successes: dict[str, str] | None = None,
    test_errors: dict[str, str] | None = None,
    test_successes: dict[str, str] | None = None,
    cluster_name_error: str | None = None,
    cluster_name_success: str | None = None,
    performance_rca_error: str | None = None,
    performance_rca_success: str | None = None,
    detail_channel: str | None = None,
) -> dict:
    """Build the shared overview/detail context from one source of truth.
    The overview carries summary state for every channel, while a detail
    response selects one channel through `detail_channel`. `errors`/
    `successes`/`test_errors`/`test_successes` are keyed by channel so one
    channel's result is never mistakenly shown on another channel's page."""
    errors = errors or {}
    successes = successes or {}
    test_errors = test_errors or {}
    test_successes = test_successes or {}

    history = _channel_history()
    channels = {}
    for key, info in _CHANNELS.items():
        channel = _fixed_channel_info(key)
        channel.update({
            "allowed_user_ids": getattr(settings, "telegram_chatbox_allowed_user_ids", "") if info.get("chatbox") else "",
            "full_access_user_ids": getattr(settings, "telegram_chatbox_full_access_user_ids", "") if info.get("chatbox") else "",
            "error": errors.get(key),
            "success": successes.get(key),
            "test_error": test_errors.get(key),
            "test_success": test_successes.get(key),
            "history": history.get(key, []),
        })
        channels[key] = channel

    rca = _fixed_channel_info(_PERFORMANCE_RCA_KEY)
    rca.update({
        "error": performance_rca_error,
        "success": performance_rca_success,
        "history": [],
    })
    channels[_PERFORMANCE_RCA_KEY] = rca

    with db.SessionLocal() as session:
        managed_rows = session.query(TelegramManagedChannel).order_by(TelegramManagedChannel.created_at.asc()).all()
        stored_order, hidden, display_names = _layout_state(session)
        for key, label in display_names.items():
            if key in channels:
                channels[key]["label"] = label
        managed = {}
        for row in managed_rows:
            channel = _public_managed_channel(row)
            managed[channel["key"]] = channel
            channels[channel["key"]] = channel

    all_keys = list(channels)
    ordered_keys = [key for key in stored_order if key in channels and key not in hidden]
    ordered_keys.extend(key for key in all_keys if key not in stored_order and key not in hidden)
    visible_channels = [channels[key] for key in ordered_keys]

    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "channels": channels,
        "visible_channels": visible_channels,
        "channel_order": ordered_keys,
        "managed_channel_keys": list(managed),
        "channel_count": len(visible_channels),
        "cluster_name": settings.cluster_name,
        "cluster_name_error": cluster_name_error,
        "cluster_name_success": cluster_name_success,
        "performance_rca_enabled": settings.telegram_performance_rca_enabled,
        "performance_rca_error": performance_rca_error,
        "performance_rca_success": performance_rca_success,
        "detail_channel": channels.get(detail_channel) if detail_channel else None,
    }


@router.get("/telegram-alerts", response_class=HTMLResponse)
async def telegram_alerts_page(request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    return templates.TemplateResponse(request, "telegram_alerts.html", _context(user))


@router.get("/telegram-alerts/help", response_class=HTMLResponse)
async def telegram_alerts_help(request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    return templates.TemplateResponse(
        request, "telegram_alerts_help.html", {"user": user, "is_admin": auth.is_admin_user(user)}
    )


def _source_credentials(source_key: str) -> tuple[str, str, str]:
    if source_key.startswith("custom:"):
        with db.SessionLocal() as session:
            source = _managed_channel_by_key(session, source_key)
            if source is None:
                raise HTTPException(status_code=404, detail="Không tìm thấy kênh nguồn")
            return source.bot_token, source.chat_id, source.template
    if source_key == _PERFORMANCE_RCA_KEY:
        return settings.telegram_incident_bot_token, settings.telegram_incident_chat_id, source_key
    info = _channel_or_404(source_key)
    return getattr(settings, info["bot_token_field"]), getattr(settings, info["chat_id_field"]), source_key


def _valid_channel_name(value: object) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(status_code=400, detail="Tên kênh là bắt buộc và tối đa 128 ký tự")
    return name


def _layout_with_changes(session, actor: str, *, order=None, hidden=None, display_names=None) -> None:
    current_order, current_hidden, current_names = _layout_state(session)
    _save_layout(
        session,
        order=order if order is not None else current_order,
        hidden=hidden if hidden is not None else current_hidden,
        display_names=display_names if display_names is not None else current_names,
        actor=actor,
    )


@router.post("/telegram-alerts/api/channels")
async def create_managed_channel(request: Request, user: str = Depends(require_login)):
    """Create a persistent custom channel without exposing copied secrets."""
    _require_admin_privilege(user)
    body = await request.json()
    name = _valid_channel_name(body.get("name"))
    source_key = str(body.get("source_id") or "").strip()
    if source_key:
        bot_token, source_chat_id, template = _source_credentials(source_key)
        chat_id = str(body.get("chat_id") or source_chat_id).strip()
    else:
        bot_token = str(body.get("bot_token") or "").strip()
        chat_id = str(body.get("chat_id") or "").strip()
        template = "custom"
    if not bot_token or not chat_id:
        raise HTTPException(status_code=400, detail="Bot Token và Chat ID là bắt buộc")

    with db.SessionLocal() as session:
        row = TelegramManagedChannel(
            name=name,
            bot_token=bot_token,
            chat_id=chat_id,
            enabled=bool(body.get("enabled", False)),
            template=template,
            created_by=user,
        )
        session.add(row)
        session.flush()
        order, hidden, names = _layout_state(session)
        key = _channel_key_for_managed(row)
        order = [item for item in order if item != key]
        order.append(key)
        _save_layout(session, order=order, hidden=hidden, display_names=names, actor=user)
        session.commit()
        result = _public_managed_channel(row)
    logger.info("telegram channel created: key=%s actor=%s", result["key"], user)
    return JSONResponse({"ok": True, "channel": result}, status_code=201)


@router.patch("/telegram-alerts/api/channels/{channel_key:path}")
async def update_managed_channel(request: Request, channel_key: str, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    body = await request.json()
    name = body.get("name")
    if name is not None:
        name = _valid_channel_name(name)
    if channel_key in _BUILTIN_CHANNEL_ORDER:
        if name is None:
            raise HTTPException(status_code=400, detail="Kênh hệ thống chỉ hỗ trợ đổi tên hiển thị")
        with db.SessionLocal() as session:
            order, hidden, names = _layout_state(session)
            names[channel_key] = name
            _save_layout(session, order=order, hidden=hidden, display_names=names, actor=user)
            session.commit()
        return {"ok": True, "key": channel_key, "name": name}

    with db.SessionLocal() as session:
        row = _managed_channel_by_key(session, channel_key)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy kênh Telegram")
        if name is not None:
            row.name = name
        if "chat_id" in body:
            row.chat_id = str(body.get("chat_id") or "").strip()
        if "bot_token" in body and str(body.get("bot_token") or "").strip():
            row.bot_token = str(body["bot_token"]).strip()
        if "enabled" in body:
            row.enabled = bool(body["enabled"])
        if not row.bot_token or not row.chat_id:
            raise HTTPException(status_code=400, detail="Bot Token và Chat ID không được để trống")
        session.commit()
        result = _public_managed_channel(row)
    return {"ok": True, "channel": result}


def _persist_fixed_enabled(channel_key: str, enabled: bool) -> None:
    if channel_key == _PERFORMANCE_RCA_KEY:
        env_config.update_env_file(
            env_config.TELEGRAM_PERFORMANCE_RCA_ENABLED_ENV_NAME,
            "true" if enabled else "false",
        )
        settings.telegram_performance_rca_enabled = enabled
        return
    info = _channel_or_404(channel_key)
    env_config.update_env_file(info["env_names"][info["enabled_field"]], "true" if enabled else "false")
    setattr(settings, info["enabled_field"], enabled)


@router.post("/telegram-alerts/api/channels/{channel_key:path}/toggle")
async def toggle_managed_channel(request: Request, channel_key: str, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    body = await request.json()
    enabled = bool(body.get("enabled"))
    if channel_key in _BUILTIN_CHANNEL_ORDER:
        _persist_fixed_enabled(channel_key, enabled)
        await _restart_channel_process(channel_key)
        return {"ok": True, "enabled": enabled}
    with db.SessionLocal() as session:
        row = _managed_channel_by_key(session, channel_key)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy kênh Telegram")
        if not row.bot_token or not row.chat_id:
            raise HTTPException(status_code=400, detail="Cấu hình Bot Token và Chat ID trước khi bật kênh")
        row.enabled = enabled
        session.commit()
    return {"ok": True, "enabled": enabled}


@router.delete("/telegram-alerts/api/channels/{channel_key:path}")
async def delete_managed_channel(request: Request, channel_key: str, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    if channel_key in _BUILTIN_CHANNEL_ORDER:
        if channel_key == _PERFORMANCE_RCA_KEY:
            _persist_fixed_enabled(channel_key, False)
        else:
            info = _channel_or_404(channel_key)
            env_config.update_env_file_batch({
                info["env_names"][info["bot_token_field"]]: "",
                info["env_names"][info["chat_id_field"]]: "",
                info["env_names"][info["enabled_field"]]: "false",
            })
            setattr(settings, info["bot_token_field"], "")
            setattr(settings, info["chat_id_field"], "")
            setattr(settings, info["enabled_field"], False)
        with db.SessionLocal() as session:
            order, hidden, names = _layout_state(session)
            hidden.add(channel_key)
            _save_layout(session, order=order, hidden=hidden, display_names=names, actor=user)
            session.commit()
        await _restart_channel_process(channel_key)
        return {"ok": True, "key": channel_key}

    with db.SessionLocal() as session:
        row = _managed_channel_by_key(session, channel_key)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy kênh Telegram")
        session.delete(row)
        order, hidden, names = _layout_state(session)
        _save_layout(session, order=[item for item in order if item != channel_key], hidden=hidden, display_names=names, actor=user)
        session.commit()
    return {"ok": True, "key": channel_key}


@router.post("/telegram-alerts/api/channels/reorder")
async def reorder_managed_channels(request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    body = await request.json()
    requested = [str(item) for item in body.get("order", [])]
    with db.SessionLocal() as session:
        current_order, hidden, names = _layout_state(session)
        current_keys = set(_BUILTIN_CHANNEL_ORDER)
        current_keys.update(_channel_key_for_managed(row) for row in session.query(TelegramManagedChannel).all())
        visible_keys = current_keys - hidden
        if len(requested) != len(set(requested)) or set(requested) != visible_keys:
            raise HTTPException(status_code=400, detail="Thứ tự kênh không hợp lệ")
        hidden_order = [item for item in current_order if item in hidden]
        _save_layout(session, order=requested + hidden_order, hidden=hidden, display_names=names, actor=user)
        session.commit()
    return {"ok": True, "order": requested}


@router.post("/telegram-alerts/api/channels/bulk")
async def bulk_manage_channels(request: Request, user: str = Depends(require_login)):
    _require_admin_privilege(user)
    body = await request.json()
    action = str(body.get("action") or "").strip().lower()
    keys = [str(item) for item in body.get("keys", [])]
    if action not in {"enable", "disable", "delete"} or not keys:
        raise HTTPException(status_code=400, detail="Bulk action không hợp lệ")
    for key in keys:
        if action == "delete":
            # Reuse the same safety semantics as a single delete while
            # keeping one request for the sticky action bar.
            if key in _BUILTIN_CHANNEL_ORDER:
                if key == _PERFORMANCE_RCA_KEY:
                    _persist_fixed_enabled(key, False)
                else:
                    info = _channel_or_404(key)
                    env_config.update_env_file_batch({
                        info["env_names"][info["bot_token_field"]]: "",
                        info["env_names"][info["chat_id_field"]]: "",
                        info["env_names"][info["enabled_field"]]: "false",
                    })
                    setattr(settings, info["bot_token_field"], "")
                    setattr(settings, info["chat_id_field"], "")
                    setattr(settings, info["enabled_field"], False)
            else:
                with db.SessionLocal() as session:
                    row = _managed_channel_by_key(session, key)
                    if row:
                        session.delete(row)
                        session.commit()
        elif key in _BUILTIN_CHANNEL_ORDER:
            _persist_fixed_enabled(key, action == "enable")
        else:
            with db.SessionLocal() as session:
                row = _managed_channel_by_key(session, key)
                if row:
                    row.enabled = action == "enable"
                    session.commit()
    if action == "delete":
        with db.SessionLocal() as session:
            order, hidden, names = _layout_state(session)
            hidden.update(keys)
            _save_layout(session, order=order, hidden=hidden, display_names=names, actor=user)
            session.commit()
    for key in keys:
        if key in _BUILTIN_CHANNEL_ORDER:
            await _restart_channel_process(key)
    return {"ok": True, "action": action, "count": len(keys)}


@router.get("/telegram-alerts/performance-rca", response_class=HTMLResponse)
async def telegram_performance_rca_page(request: Request, user: str = Depends(require_login)):
    """Render the focused settings page for the ninth, derived RCA channel.

    Performance RCA deliberately reuses the Incident channel credentials, so
    this page exposes its own lifecycle toggle and links to Incident for the
    shared Bot Token/Chat ID configuration.
    """
    _require_admin_privilege(user)
    return templates.TemplateResponse(
        request,
        "telegram_performance_rca.html",
        _context(user),
    )


@router.get("/telegram-alerts/{channel}", response_class=HTMLResponse)
async def telegram_alert_channel_page(request: Request, channel: str, user: str = Depends(require_login)):
    """Render the focused configuration page for one Telegram channel.

    The overview intentionally contains no per-channel credential form. The
    existing POST routes remain unchanged; this GET route only changes where
    operators edit a channel.
    """
    _channel_or_404(channel)
    _require_admin_privilege(user)
    return templates.TemplateResponse(
        request,
        "telegram_alert_channel.html",
        _context(user, detail_channel=channel),
    )


@router.post("/telegram-alerts/cluster-name", response_class=HTMLResponse)
async def telegram_cluster_name_submit(
    request: Request,
    user: str = Depends(require_login),
    cluster_name: str = Form(""),
):
    """Lưu tên cụm — chèn vào ĐẦU mọi tin nhắn của cả 3 kênh bên dưới (xem
    settings.cluster_name/shared/telegram_alerts.py::_with_cluster_prefix)
    để phân biệt cụm nào gửi cảnh báo khi nhiều cụm cùng trỏ về chung 1 chat
    Telegram. Khác với Bot Token/Chat ID của từng kênh (chỉ restart ĐÚNG 1
    tiến trình đọc đúng kênh đó) — giá trị này được đọc bởi CẢ Watcher
    (shared/telegram_alerts.py) LẪN Worker (worker/backup/alerting.py), nên
    phải restart cả hai để áp dụng ngay. Dashboard's own
    telegram_approval_bot.py đọc `settings` trực tiếp trong cùng tiến
    trình, không cần restart."""
    _require_admin_privilege(user)
    new_value = cluster_name.strip()

    try:
        env_config.update_env_file(CLUSTER_NAME_ENV_NAME, new_value)
        settings.cluster_name = new_value
    except Exception:
        logger.exception("telegram_cluster_name_submit: failed to persist cluster_name to .env")
        return templates.TemplateResponse(
            request,
            "telegram_alerts.html",
            _context(user, cluster_name_error="Không ghi được file cấu hình — kiểm tra quyền ghi trên server"),
        )

    await asyncio.to_thread(restart_watcher)
    await asyncio.to_thread(restart_worker)

    return templates.TemplateResponse(
        request,
        "telegram_alerts.html",
        _context(user, cluster_name_success="Đã lưu — Watcher và Worker đã khởi động lại để áp dụng ngay."),
    )


@router.post("/telegram-alerts/{channel}", response_class=HTMLResponse)
async def telegram_channel_submit(
    request: Request,
    channel: str,
    user: str = Depends(require_login),
    bot_token: str = Form(""),
    chat_id: str = Form(""),
    allowed_user_ids: str = Form(""),
    full_access_user_ids: str = Form(""),
):
    """Lưu Bot Token/Chat ID cho ĐÚNG 1 kênh. `bot_token` bỏ trống khi Lưu
    nghĩa là GIỮ NGUYÊN token đã lưu (cùng posture "blank submit = keep
    saved value" như router_api_key/backup target S3 secrets ở
    dashboard/routes/settings.py), không phải xoá — trang này không bao
    giờ render token thật ra HTML để giữ trống có ý nghĩa. Chỉ restart
    ĐÚNG tiến trình đọc kênh này (Worker cho Backup; Watcher cho Lỗi cụm/
    Phần cứng) — không đụng tới tiến trình còn lại, khác thiết kế cũ luôn
    restart cả 2."""
    info = _channel_or_404(channel)
    _require_admin_privilege(user)

    token_field = info["bot_token_field"]
    chat_field = info["chat_id_field"]
    new_bot_token = bot_token.strip() or getattr(settings, token_field)
    new_chat_id = chat_id.strip()
    new_allowed_user_ids = allowed_user_ids.strip()
    new_full_access_user_ids = full_access_user_ids.strip()
    if info.get("chatbox") and new_allowed_user_ids:
        values = [item.strip() for item in new_allowed_user_ids.split(",") if item.strip()]
        if not values or any(not re.fullmatch(r"[0-9]{1,20}", item) for item in values):
            return templates.TemplateResponse(
                request,
                "telegram_alert_channel.html",
                _context(user, errors={channel: "Allowed User ID phải là các số Telegram, ngăn cách bằng dấu phẩy"}, detail_channel=channel),
            )
        new_allowed_user_ids = ",".join(dict.fromkeys(values))
    if info.get("chatbox") and new_full_access_user_ids:
        values = [item.strip() for item in new_full_access_user_ids.split(",") if item.strip()]
        if not values or any(not re.fullmatch(r"[0-9]{1,20}", item) for item in values):
            return templates.TemplateResponse(
                request,
                "telegram_alert_channel.html",
                _context(user, errors={channel: "Full Access User ID phải là các số Telegram, ngăn cách bằng dấu phẩy"}, detail_channel=channel),
            )
        new_full_access_user_ids = ",".join(dict.fromkeys(values))

    try:
        env_fields = {
            info["env_names"][token_field]: new_bot_token,
            info["env_names"][chat_field]: new_chat_id,
        }
        if info.get("chatbox"):
            env_fields[info["env_names"]["telegram_chatbox_allowed_user_ids"]] = new_allowed_user_ids
            env_fields[info["env_names"]["telegram_chatbox_full_access_user_ids"]] = new_full_access_user_ids
        env_config.update_env_file_batch(
            env_fields
        )
        setattr(settings, token_field, new_bot_token)
        setattr(settings, chat_field, new_chat_id)
        if info.get("chatbox"):
            settings.telegram_chatbox_allowed_user_ids = new_allowed_user_ids
            settings.telegram_chatbox_full_access_user_ids = new_full_access_user_ids
    except Exception:
        logger.exception("telegram_channel_submit: failed to persist config to .env for channel %s", channel)
        return templates.TemplateResponse(
            request,
            "telegram_alert_channel.html",
            _context(user, errors={channel: "Không ghi được file cấu hình — kiểm tra quyền ghi trên server"}, detail_channel=channel),
        )

    _record_channel_config_change(channel, new_chat_id, new_bot_token, user)

    if info["restart"] == "worker":
        restart_label = "Worker"
        await asyncio.to_thread(restart_worker)
    elif info["restart"] == "watcher":
        restart_label = "Watcher"
        await asyncio.to_thread(restart_watcher)
    else:
        restart_label = "không cần restart dịch vụ"

    return templates.TemplateResponse(
        request,
        "telegram_alert_channel.html",
        _context(user, detail_channel=channel, successes={channel: (
            f"Đã lưu — {restart_label} đã khởi động lại để áp dụng ngay."
            if info["restart"] != "none" else
            "Đã lưu — lượt Code Repair kế tiếp sẽ dùng cấu hình này."
        )}),
    )


@router.post("/telegram-alerts/performance-rca/toggle", response_class=HTMLResponse)
async def performance_rca_toggle(
    request: Request,
    user: str = Depends(require_login),
    enabled: str = Form(...),
):
    """Toggle only Performance RCA candidate alerts.

    RCA uses the incident channel's saved Bot Token/Chat ID, but its alert
    lifecycle can be silenced independently from ordinary incident alerts.
    """
    _require_admin_privilege(user)
    new_enabled = enabled.strip().lower() == "true"

    try:
        env_config.update_env_file(
            env_config.TELEGRAM_PERFORMANCE_RCA_ENABLED_ENV_NAME,
            "true" if new_enabled else "false",
        )
        settings.telegram_performance_rca_enabled = new_enabled
    except Exception:
        logger.exception("performance_rca_toggle: failed to persist setting to .env")
        message = "Không ghi được file cấu hình — kiểm tra quyền ghi trên server"
        if _wants_json(request):
            return JSONResponse({"detail": message}, status_code=500)
        return templates.TemplateResponse(
            request,
            "telegram_alerts.html",
            _context(
                user,
                performance_rca_error=message,
            ),
        )

    await asyncio.to_thread(restart_watcher)
    state_label = "Đã bật" if new_enabled else "Đã tắt"
    if _wants_json(request):
        return JSONResponse({"enabled": new_enabled})
    return templates.TemplateResponse(
        request,
        "telegram_alerts.html",
        _context(
            user,
            performance_rca_success=f"{state_label} cảnh báo Performance RCA — Watcher đã khởi động lại để áp dụng ngay.",
        ),
    )


@router.post("/telegram-alerts/{channel}/toggle", response_class=HTMLResponse)
async def telegram_channel_toggle(
    request: Request,
    channel: str,
    user: str = Depends(require_login),
    enabled: str = Form(...),
):
    """2026-08-07: Bật/Tắt riêng cho ĐÚNG 1 kênh — KHÔNG đụng tới Bot Token/
    Chat ID đã lưu, chỉ lật cờ `*_enabled`. Cùng "nút luôn gửi giá trị
    -- 1 request F5/double-submit lại trang cũ không vô tình lật lại lần
    nữa vì form đó đã render giá trị mới rồi.

    Restart cùng tiến trình như Lưu Bot Token/Chat ID ở trên (Worker cho
    Backup; Watcher cho Lỗi cụm/Phần cứng) -- Watcher/Worker là tiến trình
    RIÊNG, chỉ đọc `settings` của chính nó lúc khởi động, nên việc Dashboard
    tự sửa `settings` trong tiến trình của MÌNH không đủ để 2 tiến trình
    kia thấy cờ mới ngay."""
    info = _channel_or_404(channel)
    _require_admin_privilege(user)

    enabled_field = info["enabled_field"]
    new_enabled = enabled.strip().lower() == "true"

    try:
        env_config.update_env_file(info["env_names"][enabled_field], "true" if new_enabled else "false")
        setattr(settings, enabled_field, new_enabled)
    except Exception:
        logger.exception("telegram_channel_toggle: failed to persist config to .env for channel %s", channel)
        message = "Không ghi được file cấu hình — kiểm tra quyền ghi trên server"
        if _wants_json(request):
            return JSONResponse({"detail": message}, status_code=500)
        return templates.TemplateResponse(
            request,
            "telegram_alert_channel.html",
            _context(user, errors={channel: message}, detail_channel=channel),
        )

    if info["restart"] == "worker":
        restart_label = "Worker"
        await asyncio.to_thread(restart_worker)
    elif info["restart"] == "watcher":
        restart_label = "Watcher"
        await asyncio.to_thread(restart_watcher)
    else:
        restart_label = "không cần restart dịch vụ"

    state_label = "Đã bật" if new_enabled else "Đã tắt"
    if _wants_json(request):
        return JSONResponse({"enabled": new_enabled})
    return templates.TemplateResponse(
        request,
        "telegram_alert_channel.html",
        _context(user, detail_channel=channel, successes={channel: (
            f"{state_label} kênh này — {restart_label} đã khởi động lại để áp dụng ngay."
            if info["restart"] != "none" else
            f"{state_label} kênh này — lượt Code Repair kế tiếp sẽ áp dụng."
        )}),
    )


@router.post("/telegram-alerts/{channel}/test", response_class=HTMLResponse)
async def telegram_channel_test(request: Request, channel: str, user: str = Depends(require_login)):
    """"Gửi thử" — gửi 1 tin nhắn thật bằng cấu hình ĐÃ LƯU của đúng kênh
    này (không phải giá trị chưa lưu trên form — lưu trước, thử sau), cùng
    posture với nút "Gửi thử" cũ ở Settings. Cố ý KHÔNG kiểm tra
    `*_enabled` — cho phép xác nhận Bot Token/Chat ID còn hoạt động ngay cả
    khi kênh đang tạm TẮT, trước khi bật lại."""
    info = _channel_or_404(channel)
    _require_admin_privilege(user)

    bot_token = getattr(settings, info["bot_token_field"])
    chat_id = getattr(settings, info["chat_id_field"])
    if not bot_token or not chat_id:
        return templates.TemplateResponse(
            request,
            "telegram_alert_channel.html",
            _context(user, test_errors={channel: "Chưa lưu Bot token / Chat ID — lưu cấu hình trước khi gửi thử"}, detail_channel=channel),
        )

    try:
        await asyncio.to_thread(
            send_telegram_message,
            bot_token,
            chat_id,
            f"✅ Ceph AIOps: tin nhắn thử — {info['label']} đang hoạt động.",
        )
    except TelegramSendError as exc:
        return templates.TemplateResponse(
            request, "telegram_alert_channel.html", _context(user, test_errors={channel: str(exc)}, detail_channel=channel)
        )

    return templates.TemplateResponse(
        request,
        "telegram_alert_channel.html",
        _context(user, test_successes={channel: "Đã gửi tin nhắn thử — kiểm tra Telegram."}, detail_channel=channel),
    )


@router.get("/api/telegram-outbox/status", response_class=JSONResponse)
async def telegram_outbox_status(user: str = Depends(require_login)):
    """Admin-only queue metrics; payloads and credentials are never returned."""
    _require_admin_privilege(user)
    return JSONResponse(telegram_outbox.delivery_stats())


@router.post("/api/telegram-outbox/replay", response_class=JSONResponse)
async def telegram_outbox_replay(request: Request, user: str = Depends(require_login)):
    """Explicitly requeue DEAD notifications, then let the worker deliver them."""
    _require_admin_privilege(user)
    try:
        payload = await request.json()
    except (TypeError, ValueError):
        payload = {}
    event_ids = payload.get("event_ids") if isinstance(payload, dict) else None
    if event_ids is not None and (
        not isinstance(event_ids, list)
        or any(not isinstance(value, str) or not value.strip() for value in event_ids)
    ):
        raise HTTPException(status_code=422, detail="event_ids must be a list of non-empty strings")
    raw_limit = payload.get("limit", 20) if isinstance(payload, dict) else 20
    try:
        limit = max(1, min(100, int(raw_limit)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="limit is invalid") from None
    replayed = telegram_outbox.replay_dead(event_ids=event_ids, limit=limit)
    return JSONResponse({"replayed": replayed, "queue": telegram_outbox.delivery_stats()})
