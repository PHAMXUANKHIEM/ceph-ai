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
from shared import db, env_config
from shared.models import TelegramChannelConfigChange
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
    "node": {
        "label": "Cảnh báo phần cứng",
        "bot_token_field": "telegram_node_bot_token",
        "chat_id_field": "telegram_node_chat_id",
        "enabled_field": "telegram_node_enabled",
        "env_names": env_config.TELEGRAM_NODE_ENV_NAMES,
        "restart": "watcher",
        "approval_enabled": True,
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
    "code-repair": {
        "label": "AI Code Repair — sửa hệ thống",
        "bot_token_field": "telegram_code_repair_bot_token",
        "chat_id_field": "telegram_code_repair_chat_id",
        "enabled_field": "telegram_code_repair_enabled",
        "env_names": env_config.TELEGRAM_CODE_REPAIR_ENV_NAMES,
        "restart": "none",
        "approval_enabled": False,
    },
}


def _channel_or_404(channel: str) -> dict:
    info = _CHANNELS.get(channel)
    if info is None:
        raise HTTPException(status_code=404, detail="Kênh Telegram không hợp lệ")
    return info


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
    "kênh này ai từng cấu hình, đổi lúc nào" on `/telegram-alerts`."""
    result: dict[str, list[TelegramChannelConfigChange]] = {key: [] for key in _CHANNELS}
    with db.SessionLocal() as session:
        rows = (
            session.query(TelegramChannelConfigChange)
            .order_by(TelegramChannelConfigChange.created_at.desc())
            .all()
        )
    for row in rows:
        bucket = result.get(row.channel)
        if bucket is not None and len(bucket) < _HISTORY_ROWS_PER_CHANNEL:
            bucket.append(row)
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

    history = _channel_history()
    channels = {}
    for key, info in _CHANNELS.items():
        bot_token = getattr(settings, info["bot_token_field"])
        enabled = getattr(settings, info["enabled_field"])
        channels[key] = {
            "key": key,
            "label": info["label"],
            "chat_id": getattr(settings, info["chat_id_field"]),
            "masked_bot_token": _mask_key(bot_token) if bot_token else None,
            "enabled": enabled,
            "approval_enabled": info["approval_enabled"],
            # "Đã tắt" is only meaningful once a channel actually HAS a
            # token+chat id -- an unconfigured channel is just "chưa cấu
            # hình", not "tắt", even though `enabled` defaults True either
            # way.
            "status_label": (
                "Chưa cấu hình" if not bot_token else "Đang bật" if enabled else "Đã tắt"
            ),
            "error": errors.get(key),
            "success": successes.get(key),
            "test_error": test_errors.get(key),
            "test_success": test_successes.get(key),
            "history": history.get(key, []),
        }

    return {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "channels": channels,
        "cluster_name": settings.cluster_name,
        "cluster_name_error": cluster_name_error,
        "cluster_name_success": cluster_name_success,
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
        "telegram_alerts.html",
        _context(user, successes={channel: (
            f"Đã lưu — {restart_label} đã khởi động lại để áp dụng ngay."
            if info["restart"] != "none" else
            "Đã lưu — lượt Code Repair kế tiếp sẽ dùng cấu hình này."
        )}),
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
        return templates.TemplateResponse(
            request,
            "telegram_alerts.html",
            _context(user, errors={channel: "Không ghi được file cấu hình — kiểm tra quyền ghi trên server"}),
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
    return templates.TemplateResponse(
        request,
        "telegram_alerts.html",
        _context(user, successes={channel: (
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
