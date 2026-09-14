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
_PRINTF_DIRECTIVE_RE = re.compile(
    r"%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.\d+|\.\*)?[hlL]?[diouxXeEfFgGcrsa%]"
)

_installed = False
_previous_factory: Callable[..., logging.LogRecord] | None = None


def redact_log_text(value: str) -> str:
    value = _TELEGRAM_URL_RE.sub(rf"\1{_REDACTED}\2", value)
    value = _TELEGRAM_PATH_RE.sub(rf"\1{_REDACTED}\2", value)
    return _TOKEN_RE.sub("<TELEGRAM_BOT_TOKEN>", value)


def _redact_log_arg(value: Any) -> Any:
    if isinstance(value, str):
        # Arguments are rendered after the format string.  Use the generic
        # marker here so a raw token cannot be confused with a literal value
        # in an already-redacted printf argument.
        return _TOKEN_RE.sub(_REDACTED, _TELEGRAM_PATH_RE.sub(
            rf"\1{_REDACTED}\2", _TELEGRAM_URL_RE.sub(
                rf"\1{_REDACTED}\2", value
            )
        ))
    if isinstance(value, tuple):
        return tuple(_redact_log_arg(item) for item in value)
    if isinstance(value, list):
        return [_redact_log_arg(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_log_arg(item) for key, item in value.items()}
    return value


def _redact_format_string(value: str) -> str:
    """Redact literal format text without consuming printf directives."""
    parts: list[str] = []
    cursor = 0
    for match in _PRINTF_DIRECTIVE_RE.finditer(value):
        parts.append(redact_log_text(value[cursor:match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(redact_log_text(value[cursor:]))
    return "".join(parts)


def _redact_record(record: logging.LogRecord) -> logging.LogRecord:
    if record.args:
        # Keep the original argument shape. Uvicorn's access formatter, for
        # example, unpacks five positional fields from record.args; replacing
        # them with an already-rendered message causes its formatter to throw
        # on every request. Redacting each value preserves all formatters.
        record.msg = _redact_format_string(record.msg) if isinstance(record.msg, str) else record.msg
        record.args = _redact_log_arg(record.args)
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
