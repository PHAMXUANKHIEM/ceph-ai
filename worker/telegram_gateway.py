"""Dedicated runtime for Telegram polling and the Telegram AI modes.

The web Dashboard must never poll Telegram when this service is active: two
``getUpdates`` consumers for one bot token cause Telegram to terminate one of
them.  This process owns the listener threads and supplies their long-lived
asyncio loop for chat, quota-login and Single Full background work.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from dashboard import telegram_approval_bot, telegram_chat
from shared.codex_app_server import codex_app_server
from shared import service_health
from shared.logging_redaction import install_logging_redaction

install_logging_redaction()


def _telegram_polling_enabled() -> bool:
    value = os.environ.get("CEPH_AI_TELEGRAM_POLLING_ENABLED", "true")
    return value.strip().lower() not in {"0", "false", "no", "off"}


async def run() -> None:
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, shutdown.set)
    if _telegram_polling_enabled():
        telegram_chat.set_dashboard_loop(loop)
        telegram_approval_bot.start()
    else:
        logging.getLogger(__name__).warning(
            "Telegram polling disabled by CEPH_AI_TELEGRAM_POLLING_ENABLED"
        )
    async def heartbeat() -> None:
        while True:
            service_health.record_safe("telegram-ai")
            await asyncio.sleep(10)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        await shutdown.wait()
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        telegram_chat.clear_dashboard_loop(loop)
        await codex_app_server.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    asyncio.run(run())
