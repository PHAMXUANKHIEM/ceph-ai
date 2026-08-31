"""Entrypoint for the daily, timer-driven AI self-improvement review."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from shared.telegram_client import TelegramSendError, send_telegram_message


def _notify_bootstrap_failure(exc: Exception) -> None:
    """Best-effort alert even when Settings itself cannot be constructed."""
    enabled = os.getenv("TELEGRAM_CODE_REPAIR_ENABLED", "true").strip().lower()
    token = os.getenv("TELEGRAM_CODE_REPAIR_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CODE_REPAIR_CHAT_ID", "").strip()
    if enabled in {"0", "false", "no", "off"} or not token or not chat_id:
        return
    try:
        send_telegram_message(
            token,
            chat_id,
            "⚠️ AI NIGHTLY IMPROVEMENT KHÔNG KHỞI ĐỘNG\n"
            f"Lỗi cấu hình/khởi động: {str(exc)[:900]}",
        )
    except TelegramSendError:
        logging.getLogger(__name__).exception("could not send nightly bootstrap failure alert")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    )
    try:
        from config.settings import settings
        from worker.code_repair_supervisor import run_nightly_ai_improvement

        if not settings.ai_nightly_improvement_enabled:
            logging.getLogger(__name__).info("nightly AI improvement is disabled")
            return 0
        completed = run_nightly_ai_improvement(
            Path(__file__).resolve().parents[1],
            Path(settings.ai_nightly_improvement_state_file),
        )
        if completed:
            return 0
        logging.getLogger(__name__).error("nightly AI improvement failed; systemd will retry")
        return 1
    except Exception as exc:
        logging.getLogger(__name__).exception("nightly AI improvement entrypoint failed")
        _notify_bootstrap_failure(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
