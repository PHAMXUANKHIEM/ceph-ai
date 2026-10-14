"""Execute approved Telegram lab proposals inside the mutation-owning Worker."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from shared import db, failure_lab_config, reproduction_approval
from shared.failure_lab_fault import notify_lab
from shared.models import TelegramManagedChannel

logger = logging.getLogger(__name__)
_active_process: subprocess.Popen[str] | None = None
_active_lock = threading.Lock()
_error_notice_lock = threading.Lock()
_error_notices: dict[str, float] = {}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|secret|private[_ -]?key|authorization|client[_ -]?secret)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def _safe_error_detail(value: object, *, limit: int = 900) -> str:
    """Build a short Telegram-safe diagnostic from CLI or exception output."""
    from shared.logging_redaction import redact_log_text

    text = redact_log_text(str(value or ""))
    text = _SECRET_ASSIGNMENT.sub(r"\1\2<đã ẩn>", text)
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-5:])[-limit:] or "Không có chi tiết lỗi từ runner."


def _notify_error_once(key: str, message: str, *, interval: float = 300) -> None:
    """Avoid flooding the lab chat when a persistent queue/config error repeats."""
    now = time.monotonic()
    with _error_notice_lock:
        if key in _error_notices and now - _error_notices[key] < interval:
            return
        _error_notices[key] = now
    notify_lab(message)


def _mark_failed(proposal_id: str, reason: str) -> None:
    """Persist failure when possible without suppressing its Telegram notice."""
    try:
        reproduction_approval.update(proposal_id, status="FAILED", error=reason[:500])
    except Exception:  # noqa: BLE001 - reporting still matters if proposal storage is degraded
        logger.exception("failure lab Telegram: could not persist FAILED for %s", proposal_id)


def _binding_error(record: dict) -> str | None:
    """Return a user-facing reason when the approved staging binding is stale."""
    try:
        config = failure_lab_config.load()
    except Exception as exc:  # noqa: BLE001 - refuse closed and give the operator the failure class
        logger.exception("failure lab Telegram: could not read Failure Lab configuration")
        return f"Không đọc được cấu hình Failure Lab ({type(exc).__name__}); không chạy fault."
    try:
        decided_at = datetime.fromisoformat(str(record["decided_at"]))
    except (KeyError, TypeError, ValueError):
        return "Thiếu hoặc sai thời điểm duyệt; hãy tạo yêu cầu mới."
    if decided_at.tzinfo is None:
        return "Thời điểm duyệt thiếu múi giờ hợp lệ; hãy tạo yêu cầu mới."
    approval_age = datetime.now(timezone.utc) - decided_at
    if approval_age < timedelta(0) or approval_age > timedelta(minutes=5):
        return "Lượt duyệt đã quá 5 phút; hãy tạo yêu cầu mới để xác nhận lại cụm."
    if not config.fault_enabled:
        return "Cờ cho phép gây lỗi đã tắt trong Cài đặt Failure Lab."
    if not config.cluster_id or not config.fsid or not config.telegram_channel_id:
        return "Chưa cấu hình đủ cụm staging, FSID ghim và kênh Telegram Failure Lab."
    if (record.get("cluster_id") != config.cluster_id or record.get("fsid") != config.fsid
            or record.get("channel_id") != config.telegram_channel_id):
        return "Cụm staging, FSID hoặc kênh Telegram đã đổi sau khi duyệt; không chạy fault."
    try:
        with db.SessionLocal() as session:
            channel = session.get(TelegramManagedChannel, config.telegram_channel_id)
    except Exception:  # noqa: BLE001 - fail closed, but explain the failed check to the operator
        logger.exception("failure lab Telegram: could not revalidate the selected channel")
        return "Không truy vấn được cấu hình kênh Telegram để xác minh an toàn; không chạy fault."
    if channel is None or channel.template != "failure-lab" or not channel.enabled:
        return "Kênh Telegram Failure Lab không tồn tại, đã tắt hoặc sai loại kênh."
    if not channel.bot_token or not channel.chat_id:
        return "Kênh Telegram Failure Lab thiếu Bot Token hoặc Chat ID."
    return None


def terminate_active() -> None:
    """Forward container SIGTERM to the CLI so its undo finally can run."""
    with _active_lock:
        process = _active_process
    if process is not None and process.poll() is None:
        process.terminate()


def _binding_valid(record: dict) -> bool:
    return _binding_error(record) is None


def run_one() -> bool:
    try:
        proposal_id = reproduction_approval.next_approved_telegram()
    except Exception as exc:  # noqa: BLE001 - keep the Worker poller alive on storage errors
        detail = _safe_error_detail(f"{type(exc).__name__}: {exc}")
        logger.exception("failure lab Telegram: could not read the approved proposal queue")
        _notify_error_once("proposal-queue", f"❌ Failure Lab không đọc được hàng đợi duyệt. Chi tiết: {detail}")
        return True
    if proposal_id is None:
        return False
    try:
        record = reproduction_approval.load(proposal_id)
    except Exception as exc:  # noqa: BLE001 - report corrupt/missing proposal instead of killing the poller
        detail = _safe_error_detail(f"{type(exc).__name__}: {exc}")
        logger.exception("failure lab Telegram: could not load approved proposal %s", proposal_id)
        _notify_error_once(proposal_id, f"❌ {proposal_id}: không đọc được nội dung đề xuất đã duyệt. Chi tiết: {detail}")
        return True
    binding_error = _binding_error(record)
    if binding_error:
        logger.warning("failure lab Telegram: approved proposal %s refused: %s", proposal_id, binding_error)
        notify_lab(f"⛔ {proposal_id}: không chạy fault. Lý do: {binding_error}")
        _mark_failed(proposal_id, binding_error)
        return True
    try:
        # The CLI owns SIGTERM/undo and writes its own report. Never inject a
        # fault inside a Worker thread that could disappear during shutdown.
        global _active_process
        with _active_lock:
            process = subprocess.Popen(  # nosec B603 - constant command and validated proposal id
                [sys.executable, "-m", "scripts.lab.failure_lab_fault", "--cluster-id", record["cluster_id"],
                 "--proposal", proposal_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            _active_process = process
        try:
            stdout, stderr = process.communicate(timeout=1800)
        except subprocess.TimeoutExpired:
            process.terminate()  # CLI SIGTERM handler unwinds through undo.
            try:
                stdout, stderr = process.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                logger.critical("failure lab Telegram: runner %s did not exit after SIGTERM; manual recovery required",
                                proposal_id)
                notify_lab(f"⛔ {proposal_id}: runner không dừng sau SIGTERM; kiểm tra cụm và hoàn tác thủ công.")
                # Keep ownership of the child and block later fault requests.
                # Releasing this task while it may still hold a fault would
                # make a second approved request race with manual recovery.
                while True:
                    try:
                        stdout, stderr = process.communicate(timeout=60)
                        break
                    except subprocess.TimeoutExpired:
                        logger.error("failure lab Telegram: runner %s still active; waiting for operator recovery",
                                     proposal_id)
        finally:
            with _active_lock:
                _active_process = None
        if process.returncode != 0:
            detail = _safe_error_detail(stderr or stdout)
            logger.error("failure lab Telegram: %s exited %s: %s", proposal_id, process.returncode, detail)
            latest = reproduction_approval.load(proposal_id)
            if latest.get("status") != "DONE":
                reason = f"Runner thoát với mã {process.returncode}: {detail}"
                notify_lab(f"⚠️ {proposal_id}: chạy không thành công. Nguyên nhân/log cuối:\n{detail}\n"
                           "Kiểm tra trạng thái cụm và report Failure Lab trước khi thử lại.")
                _mark_failed(proposal_id, reason)
        else:
            logger.info("failure lab Telegram: %s finished: %s", proposal_id, stdout.strip()[:500])
    except Exception as exc:
        logger.exception("failure lab Telegram: approved run %s failed", proposal_id)
        detail = _safe_error_detail(f"{type(exc).__name__}: {exc}")
        notify_lab(f"❌ {proposal_id}: lỗi nội bộ Worker khi khởi chạy/thu kết quả. Chi tiết: {detail}\n"
                   "Kiểm tra trạng thái cụm lab trước khi thử lại.")
        _mark_failed(proposal_id, detail)
    return True


async def run() -> None:
    while True:
        processed = await asyncio.to_thread(run_one)
        await asyncio.sleep(2 if processed else 10)
