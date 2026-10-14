"""Incoming, approval-gated AI fault requests for the dedicated lab chat."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time

from config.settings import settings
from shared import db, failure_lab_config, reproduction_approval
from shared.failure_lab_chat import LabChatRefused, card_text, prompt_for, proposal_from_answer
from shared.models import TelegramManagedChannel
from shared.telegram_client import edit_telegram_message, send_telegram_message, send_telegram_message_with_keyboard

logger = logging.getLogger(__name__)
APPROVE_PREFIX = "fllab:ok:"
SKIP_PREFIX = "fllab:skip:"
CALLBACK_PREFIXES = (APPROVE_PREFIX, SKIP_PREFIX)
_messages: queue.Queue[tuple[dict, str]] = queue.Queue(maxsize=3)
_worker_lock = threading.Lock()
_worker_started = False
_token_cache: tuple[float, str | None] = (0.0, None)
_token_cache_lock = threading.Lock()


def destination() -> tuple[str, str, str] | None:
    """Resolve credentials on every use; never fall back to a production bot."""
    channel_id = failure_lab_config.load().telegram_channel_id
    if not channel_id:
        return None
    try:
        with db.SessionLocal() as session:
            row = session.get(TelegramManagedChannel, channel_id)
            if row is None or row.template != "failure-lab" or not row.enabled or not row.bot_token or not row.chat_id:
                return None
            return row.id, row.bot_token, str(row.chat_id)
    except Exception:
        logger.exception("failure lab Telegram: cannot resolve selected channel")
        return None


def configured_token() -> str | None:
    global _token_cache
    with _token_cache_lock:
        checked_at, token = _token_cache
        now = time.monotonic()
        if now - checked_at < 30:
            return token
        target = destination()
        token = target[1] if target else None
        _token_cache = (now, token)
        return token


def _operator_allowed(update: dict) -> bool:
    """A dedicated explicit allow-list; group-wide operator mode is NOT enough."""
    sender = update.get("from") or {}
    sender_id = str(sender.get("id") or "")
    raw = str(settings.telegram_chatbox_full_access_user_ids or "")
    allowed = {value.strip() for value in raw.split(",") if value.strip().isdigit()}
    return bool(sender_id.isdigit() and not sender.get("is_bot") and sender_id in allowed)


def is_lab_destination(message: dict, token: str) -> bool:
    target = destination()
    return bool(target and (token, str((message.get("chat") or {}).get("id"))) == target[1:])


def is_lab_message(message: dict, token: str) -> bool:
    return is_lab_destination(message, token) and _operator_allowed(message)


def is_lab_callback(callback: dict, token: str) -> bool:
    message = callback.get("message") or {}
    return is_lab_message({"chat": message.get("chat"), "from": callback.get("from")}, token)


def _create_request(text: str, actor: str, channel_id: str) -> dict:
    from scripts.lab.propose_reproduction import ask_ai

    config = failure_lab_config.load()
    if not config.cluster_id or not config.fsid or config.telegram_channel_id != channel_id:
        raise LabChatRefused("Chưa chọn cụm staging, ghim FSID và kênh Telegram hợp lệ.")
    with db.SessionLocal() as session:
        from shared.models import Cluster

        cluster = session.get(Cluster, config.cluster_id)
        if cluster is None or not cluster.is_active or cluster.autonomy_environment != "lab":
            raise LabChatRefused("Cụm staging không còn active hoặc chưa được đánh dấu lab.")
        name = cluster.name
    answer = ask_ai(prompt_for(text), timeout_seconds=120)
    record = proposal_from_answer(text, answer, actor=actor, cluster_id=config.cluster_id,
                                  cluster_name=name, fsid=config.fsid, channel_id=channel_id)
    reproduction_approval.create(record)
    return record


async def _handle_message(message: dict, token: str) -> None:
    if not is_lab_message(message, token):
        return
    target = destination()
    if target is None:
        return
    channel_id, _bot, chat_id = target
    text = str(message.get("text") or "").strip()
    if text.startswith("/start") or text.startswith("/help"):
        await asyncio.to_thread(send_telegram_message, token, chat_id,
                                "Gửi /lab <mô tả lỗi> để AI chọn một kịch bản Failure Lab. "
                                "AI không thực thi trực tiếp; operator phải duyệt trên Telegram.")
        return
    if text.startswith("/lab"):
        text = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
    if not text or len(text) > 2000:
        await asyncio.to_thread(send_telegram_message, token, chat_id,
                                "Gửi mô tả lỗi từ 1 đến 2000 ký tự, ví dụ: /lab mô phỏng OSD down.")
        return
    actor = f"telegram-lab:{(message.get('from') or {}).get('id')}"
    try:
        record = await asyncio.to_thread(_create_request, text, actor, channel_id)
        current = destination()
        if current != target or failure_lab_config.load().cluster_id != record["cluster_id"]:
            reproduction_approval.update(record["id"], status="SKIPPED", error="Lab channel/cluster changed")
            return
        await asyncio.to_thread(send_telegram_message_with_keyboard, token, chat_id, card_text(record), [
            ("✅ Duyệt gây lỗi", f"{APPROVE_PREFIX}{record['id']}"),
            ("⏭ Bỏ qua", f"{SKIP_PREFIX}{record['id']}"),
        ])
    except LabChatRefused as exc:
        await asyncio.to_thread(send_telegram_message, token, chat_id, f"⛔ {exc}")
    except Exception:
        logger.exception("failure lab Telegram: AI proposal failed")
        await asyncio.to_thread(send_telegram_message, token, chat_id,
                                "Không tạo được đề xuất Failure Lab; chưa có thao tác nào được thực hiện.")


def _message_worker() -> None:
    while True:
        message, token = _messages.get()
        try:
            asyncio.run(_handle_message(message, token))
        except Exception:
            logger.exception("failure lab Telegram: message worker failed")
        finally:
            _messages.task_done()


def enqueue_message(message: dict, token: str) -> bool:
    if not is_lab_message(message, token):
        return False
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_message_worker, daemon=True, name="failure-lab-telegram").start()
            _worker_started = True
    try:
        _messages.put_nowait((message, token))
    except queue.Full:
        send_telegram_message(token, str((message.get("chat") or {}).get("id")),
                              "Failure Lab đang xử lý nhiều yêu cầu; hãy thử lại sau.")
    return True


def handle_callback(callback: dict, token: str) -> str:
    if not is_lab_callback(callback, token):
        return "Chỉ operator được cấp quyền trên kênh Failure Lab mới có thể duyệt."
    data = str(callback.get("data") or "")
    approve = data.startswith(APPROVE_PREFIX)
    prefix = APPROVE_PREFIX if approve else SKIP_PREFIX
    try:
        proposal_id = reproduction_approval.parse(data, prefix)
        record = reproduction_approval.load(proposal_id)
        config = failure_lab_config.load()
        target = destination()
        if record.get("source") != "telegram_failure_lab" or target is None:
            return "Đề xuất không thuộc kênh Failure Lab hiện tại."
        if record.get("channel_id") != target[0] or record.get("cluster_id") != config.cluster_id or record.get("fsid") != config.fsid:
            return "Cụm, FSID hoặc kênh đã đổi; hãy tạo đề xuất mới."
        if approve and not config.fault_enabled:
            return "Chưa bật gây lỗi trong Cài đặt > Cụm Staging; chưa thực hiện."
        sender_id = str((callback.get("from") or {}).get("id"))
        reproduction_approval.decide(proposal_id, approve=approve, actor=f"telegram-lab:{sender_id}")
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id"))
        message_id = message.get("message_id")
        note = ("\n\n✅ Đã duyệt; Worker sẽ kiểm tra chốt an toàn rồi chạy một lần."
                if approve else "\n\n⏭ Đã bỏ qua; không gây lỗi.")
        try:
            if isinstance(message_id, int):
                edit_telegram_message(token, chat_id, message_id,
                                      (str(message.get("text") or "") + note)[:4000])
        except Exception:
            logger.exception("failure lab Telegram: cannot edit approval card")
        return f"Đã {'duyệt' if approve else 'bỏ qua'} {proposal_id}."
    except reproduction_approval.ReproductionError as exc:
        return str(exc)
