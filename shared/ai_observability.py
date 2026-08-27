"""Best-effort, content-free telemetry for logical AI calls."""
from __future__ import annotations

import functools
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from config.settings import settings
from shared import db
from shared.models import AIInvocation

logger = logging.getLogger(__name__)


def _content_size(value: Any, *, depth: int = 0) -> int:
    """Count payload characters without serializing or retaining its content."""
    if depth > 8 or value is None:
        return 0
    if isinstance(value, (str, bytes, bytearray)):
        return len(value)
    if isinstance(value, dict):
        return sum(_content_size(k, depth=depth + 1) + _content_size(v, depth=depth + 1) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return sum(_content_size(item, depth=depth + 1) for item in value)
    if isinstance(value, (bool, int, float)):
        return len(str(value))
    return 0


def _provider_and_model(scope: str) -> tuple[str, str]:
    prefix = "vitastor_" if scope == "vitastor" else ""
    if getattr(settings, f"{prefix}codex_chat_enabled", False):
        return "codex", getattr(settings, f"{prefix}codex_chat_model", "") or "default"
    if getattr(settings, f"{prefix}claude_chat_enabled", False):
        return "claude", getattr(settings, f"{prefix}claude_chat_model", "") or "default"
    return (
        getattr(settings, f"{prefix}router_provider", "unknown") or "unknown",
        getattr(settings, f"{prefix}router_model", "") or "default",
    )


def _record(**values: Any) -> None:
    try:
        with db.SessionLocal() as session:
            session.add(AIInvocation(id=str(uuid.uuid4()), created_at=datetime.utcnow(), **values))
            session.commit()
    except Exception:
        # Telemetry must never change the result of an AI operation. No input,
        # output or exception message is included in this log record.
        logger.warning("Unable to persist AI invocation telemetry", exc_info=True)


def observe_ai_call(feature: str, *, scope: str = "ceph") -> Callable:
    """Instrument an async logical AI call without storing its content."""
    def decorate(function: Callable) -> Callable:
        @functools.wraps(function)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            input_chars = _content_size(args) + _content_size(kwargs)
            provider, model_id = _provider_and_model(scope)
            try:
                result = await function(*args, **kwargs)
            except Exception as exc:
                _record(
                    feature=feature, provider=provider, model_id=model_id,
                    status="ERROR", latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                    input_chars=input_chars, output_chars=0, error_type=type(exc).__name__,
                )
                raise
            _record(
                feature=feature, provider=provider, model_id=model_id,
                status="SUCCESS", latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                input_chars=input_chars, output_chars=_content_size(result), error_type=None,
            )
            return result
        return wrapped
    return decorate
