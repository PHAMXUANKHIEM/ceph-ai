"""Execute approved Telegram lab proposals inside the mutation-owning Worker."""

from __future__ import annotations

import asyncio
import logging
import subprocess  # nosec B404 - fixed argv, no shell
import sys
import threading
from datetime import datetime, timedelta, timezone

from shared import db, failure_lab_config, reproduction_approval
from shared.failure_lab_fault import notify_lab
from shared.models import TelegramManagedChannel

logger = logging.getLogger(__name__)
_active_process: subprocess.Popen[str] | None = None
_active_lock = threading.Lock()


def terminate_active() -> None:
    """Forward container SIGTERM to the CLI so its undo finally can run."""
    with _active_lock:
        process = _active_process
    if process is not None and process.poll() is None:
        process.terminate()


def _binding_valid(record: dict) -> bool:
    config = failure_lab_config.load()
    try:
        decided_at = datetime.fromisoformat(str(record["decided_at"]))
    except (KeyError, ValueError):
        return False
    if decided_at.tzinfo is None or datetime.now(timezone.utc) - decided_at > timedelta(minutes=5):
        return False
    if (not config.fault_enabled or not config.cluster_id or not config.fsid or not config.telegram_channel_id
            or record.get("cluster_id") != config.cluster_id or record.get("fsid") != config.fsid
            or record.get("channel_id") != config.telegram_channel_id):
        return False
    with db.SessionLocal() as session:
        channel = session.get(TelegramManagedChannel, config.telegram_channel_id)
        return bool(channel and channel.template == "failure-lab" and channel.enabled
                    and channel.bot_token and channel.chat_id)


def run_one() -> bool:
    proposal_id = reproduction_approval.next_approved_telegram()
    if proposal_id is None:
        return False
    record = reproduction_approval.load(proposal_id)
    if not _binding_valid(record):
        reproduction_approval.update(proposal_id, status="FAILED", error="Lab binding or fault gate changed")
        logger.warning("failure lab Telegram: approved proposal %s refused because binding changed", proposal_id)
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
            logger.error("failure lab Telegram: %s exited %s: %s", proposal_id, process.returncode,
                         (stderr or stdout)[-1000:])
            latest = reproduction_approval.load(proposal_id)
            if latest.get("status") != "DONE":
                reproduction_approval.update(proposal_id, status="FAILED", error="runner returned nonzero")
                notify_lab(f"⚠️ {proposal_id}: runner kết thúc không thành công; kiểm tra báo cáo và cụm.")
        else:
            logger.info("failure lab Telegram: %s finished: %s", proposal_id, stdout.strip()[:500])
    except Exception:
        logger.exception("failure lab Telegram: approved run %s failed", proposal_id)
        reproduction_approval.update(proposal_id, status="FAILED", error="internal runner error")
        notify_lab(f"❌ {proposal_id} thất bại; kiểm tra log Worker và trạng thái phục hồi cụm lab.")
    return True


async def run() -> None:
    while True:
        processed = await asyncio.to_thread(run_one)
        await asyncio.sleep(2 if processed else 10)
