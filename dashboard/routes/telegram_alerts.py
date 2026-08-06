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
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.settings import _mask_key, _require_admin_privilege, restart_watcher, restart_worker
from dashboard.templating import make_templates
from shared import env_config
from shared.telegram_client import TelegramSendError, send_telegram_message

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# Single source of truth for the 3 channels this page manages — label,
# which config.settings.Settings fields back it, which .env variable names
# shared/env_config.py maps those fields to, and which process actually
# reads it (so a save here restarts ONLY that process, not both like the
# old shared-config design always did).
_CHANNELS: dict[str, dict] = {
    "backup": {
        "label": "Cảnh báo Backup",
        "bot_token_field": "telegram_backup_bot_token",
        "chat_id_field": "telegram_backup_chat_id",
        "env_names": env_config.TELEGRAM_BACKUP_ENV_NAMES,
        "restart": "worker",
    },
    "incident": {
        "label": "Cảnh báo lỗi cụm",
        "bot_token_field": "telegram_incident_bot_token",
        "chat_id_field": "telegram_incident_chat_id",
        "env_names": env_config.TELEGRAM_INCIDENT_ENV_NAMES,
        "restart": "watcher",
    },
    "node": {
        "label": "Cảnh báo phần cứng",
        "bot_token_field": "telegram_node_bot_token",
        "chat_id_field": "telegram_node_chat_id",
        "env_names": env_config.TELEGRAM_NODE_ENV_NAMES,
        "restart": "watcher",
    },
}


def _channel_or_404(channel: str) -> dict:
    info = _CHANNELS.get(channel)
    if info is None:
        raise HTTPException(status_code=404, detail="Kênh Telegram không hợp lệ")
    return info


def _context(
    user: str,
    *,
    errors: dict[str, str] | None = None,
    successes: dict[str, str] | None = None,
    test_errors: dict[str, str] | None = None,
    test_successes: dict[str, str] | None = None,
) -> dict:
    """Every one of the 3 channel forms renders from this single
    telegram_alerts.html — every response must carry every channel's
    current values, or Jinja2 silently renders the missing ones blank
    instead of showing the OTHER channels' saved state. `errors`/
    `successes`/`test_errors`/`test_successes` are keyed by channel so one
    channel's result is never mistakenly shown on another's card."""
    errors = errors or {}
    successes = successes or {}
    test_errors = test_errors or {}
    test_successes = test_successes or {}

    channels = {}
    for key, info in _CHANNELS.items():
        bot_token = getattr(settings, info["bot_token_field"])
        channels[key] = {
            "key": key,
            "label": info["label"],
            "chat_id": getattr(settings, info["chat_id_field"]),
            "masked_bot_token": _mask_key(bot_token) if bot_token else None,
            "error": errors.get(key),
            "success": successes.get(key),
            "test_error": test_errors.get(key),
            "test_success": test_successes.get(key),
        }

    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "channels": channels,
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


@router.post("/telegram-alerts/{channel}", response_class=HTMLResponse)
async def telegram_channel_submit(
    request: Request,
    channel: str,
    user: str = Depends(require_login),
    bot_token: str = Form(""),
    chat_id: str = Form(""),
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

    try:
        env_config.update_env_file_batch(
            {
                info["env_names"][token_field]: new_bot_token,
                info["env_names"][chat_field]: new_chat_id,
            }
        )
        setattr(settings, token_field, new_bot_token)
        setattr(settings, chat_field, new_chat_id)
    except Exception:
        logger.exception("telegram_channel_submit: failed to persist config to .env for channel %s", channel)
        return templates.TemplateResponse(
            request,
            "telegram_alerts.html",
            _context(user, errors={channel: "Không ghi được file cấu hình — kiểm tra quyền ghi trên server"}),
        )

    if info["restart"] == "worker":
        restart_label = "Worker"
        await asyncio.to_thread(restart_worker)
    else:
        restart_label = "Watcher"
        await asyncio.to_thread(restart_watcher)

    return templates.TemplateResponse(
        request,
        "telegram_alerts.html",
        _context(user, successes={channel: f"Đã lưu — {restart_label} đã khởi động lại để áp dụng ngay."}),
    )


@router.post("/telegram-alerts/{channel}/test", response_class=HTMLResponse)
async def telegram_channel_test(request: Request, channel: str, user: str = Depends(require_login)):
    """"Gửi thử" — gửi 1 tin nhắn thật bằng cấu hình ĐÃ LƯU của đúng kênh
    này (không phải giá trị chưa lưu trên form — lưu trước, thử sau), cùng
    posture với nút "Gửi thử" cũ ở Settings."""
    info = _channel_or_404(channel)
    _require_admin_privilege(user)

    bot_token = getattr(settings, info["bot_token_field"])
    chat_id = getattr(settings, info["chat_id_field"])
    if not bot_token or not chat_id:
        return templates.TemplateResponse(
            request,
            "telegram_alerts.html",
            _context(user, test_errors={channel: "Chưa lưu Bot token / Chat ID — lưu cấu hình trước khi gửi thử"}),
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
            request, "telegram_alerts.html", _context(user, test_errors={channel: str(exc)})
        )

    return templates.TemplateResponse(
        request,
        "telegram_alerts.html",
        _context(user, test_successes={channel: "Đã gửi tin nhắn thử — kiểm tra Telegram."}),
    )
