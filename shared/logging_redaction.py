"""Process-wide logging redaction for secrets that may appear in library logs."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

_REDACTED = "<REDACTED>"
_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_TELEGRAM_URL_RE = re.compile(
    r"(https?://api\.telegram\.org/bot)"
    r"[^/\s]+"
    r"(/(?:sendMessage|editMessageText|getUpdates|answerCallbackQuery|setMyCommands)\b)"
)
_TELEGRAM_PATH_RE = re.compile(
    r"(/bot)"
    r"[^/\s]+"
    r"(/(?:sendMessage|editMessageText|getUpdates|answerCallbackQuery|setMyCommands)\b)"
)

_installed = False
_previous_factory: Callable[..., logging.LogRecord] | None = None


def redact_log_text(value: str) -> str:
    value = _TELEGRAM_URL_RE.sub(rf"\1{_REDACTED}\2", value)
    value = _TELEGRAM_PATH_RE.sub(rf"\1{_REDACTED}\2", value)
    return _TOKEN_RE.sub("<TELEGRAM_BOT_TOKEN>", value)


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    if record.args:
        record.msg = redact_log_text(record.getMessage())
        record.args = ()
    elif isinstance(record.msg, str):
        record.msg = redact_log_text(record.msg)
    return record


def install_logging_redaction() -> None:
    """Install once per process and quiet HTTP client request logging."""
    global _installed, _previous_factory
    if _installed:
        return
    _previous_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return _redact_record(_previous_factory(*args, **kwargs))  # type: ignore[misc]

    logging.setLogRecordFactory(factory)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _installed = True
