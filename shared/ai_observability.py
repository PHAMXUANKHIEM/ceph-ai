"""Best-effort, content-free telemetry for logical AI calls."""
from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable

from config.settings import settings
from shared import db
from shared.ai_budget import AIBudgetError, check as check_ai_budget
from shared.models import AIInvocation

logger = logging.getLogger(__name__)

_USAGE_CONTEXT: ContextVar[dict | None] = ContextVar("ai_usage_context", default=None)


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, count)


def _read_field(value: Any, names: tuple[str, ...]) -> int | None:
    for name in names:
        raw = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        count = _token_count(raw)
        if count is not None:
            return count
    return None


def _usage_counts(value: Any) -> tuple[int | None, int | None]:
    usage = value.get("usage") if isinstance(value, dict) else getattr(value, "usage", None)
    source = usage if usage is not None else value
    input_tokens = _read_field(source, ("prompt_tokens", "input_tokens", "promptTokens", "inputTokens"))
    output_tokens = _read_field(source, ("completion_tokens", "output_tokens", "completionTokens", "outputTokens"))
    return input_tokens, output_tokens


def record_ai_usage(value: Any = None, *, input_tokens: int | None = None, output_tokens: int | None = None) -> None:
    """Accumulate provider-reported usage for the surrounding decorated call.

    Provider adapters call this after receiving a response. The values are
    content-free and are ignored when the call is not wrapped by the
    observability decorator.
    """
    state = _USAGE_CONTEXT.get()
    if state is None:
        return
    detected_input, detected_output = _usage_counts(value)
    input_tokens = _token_count(input_tokens) if input_tokens is not None else detected_input
    output_tokens = _token_count(output_tokens) if output_tokens is not None else detected_output
    if input_tokens is not None:
        state["input_tokens"] = (state["input_tokens"] or 0) + input_tokens
    if output_tokens is not None:
        state["output_tokens"] = (state["output_tokens"] or 0) + output_tokens


def _recorded_usage() -> tuple[int | None, int | None]:
    state = _USAGE_CONTEXT.get() or {}
    return state.get("input_tokens"), state.get("output_tokens")


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


def _provider_and_model(scope: str, backend: str) -> tuple[str, str]:
    prefix = "vitastor_" if scope == "vitastor" else ""
    if backend == "codex" or (
        backend == "configured" and getattr(settings, f"{prefix}codex_chat_enabled", False)
    ):
        return "codex", getattr(settings, f"{prefix}codex_chat_model", "") or "default"
    if backend == "claude" or (
        backend == "configured" and getattr(settings, f"{prefix}claude_chat_enabled", False)
    ):
        return "claude", getattr(settings, f"{prefix}claude_chat_model", "") or "default"
    return (
        getattr(settings, f"{prefix}router_provider", "unknown") or "unknown",
        getattr(settings, f"{prefix}router_model", "") or "default",
    )


def _record(*, reservation_id: str | None = None, **values: Any) -> None:
    try:
        with db.SessionLocal() as session:
            if reservation_id:
                row = session.get(AIInvocation, reservation_id)
            else:
                row = None
            if row is None:
                session.add(AIInvocation(id=str(uuid.uuid4()), created_at=datetime.utcnow(), **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            session.commit()
    except Exception:
        # Telemetry must never change the result of an AI operation. No input,
        # output or exception message is included in this log record.
        logger.warning("Unable to persist AI invocation telemetry", exc_info=True)


def record_ai_attempt(
    *, reservation_id: str | None, feature: str, provider: str, model_id: str,
    status: str, latency_ms: int, input_chars: int, output_chars: int,
    error_type: str | None = None,
) -> None:
    """Persist a CLI-backed AI attempt using the same accounting row as the decorator.

    CLI providers do not return an OpenAI usage object, so their token fields
    remain estimated from character counts by the cost layer. Keeping this
    public avoids making callers reach into the decorator's private recorder.
    """
    _record(
        reservation_id=reservation_id,
        feature=feature,
        provider=provider,
        model_id=model_id,
        status=status,
        latency_ms=max(0, int(latency_ms)),
        input_chars=max(0, int(input_chars)),
        output_chars=max(0, int(output_chars)),
        input_tokens=None,
        output_tokens=None,
        error_type=error_type,
    )


def observe_ai_call(
    feature: str,
    *,
    scope: str = "ceph",
    backend: str = "configured",
    when: Callable[..., bool] | None = None,
) -> Callable:
    """Instrument an async logical AI call without storing its content."""
    def decorate(function: Callable) -> Callable:
        @functools.wraps(function)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            if when is not None and not when(*args, **kwargs):
                return await function(*args, **kwargs)
            started = time.monotonic()
            input_chars = _content_size(args) + _content_size(kwargs)
            provider, model_id = _provider_and_model(scope, backend)
            reservation_id = None
            usage_token = _USAGE_CONTEXT.set({"input_tokens": None, "output_tokens": None})
            try:
                try:
                    reservation_id = check_ai_budget(provider, model_id, input_chars)
                    result = await function(*args, **kwargs)
                    # A small adapter may return the raw provider response;
                    # capture its usage automatically in addition to the
                    # explicit hooks used by streaming call sites.
                    record_ai_usage(result)
                except Exception as exc:
                    actual_input_tokens, actual_output_tokens = _recorded_usage()
                    await asyncio.to_thread(
                        _record,
                        reservation_id=reservation_id,
                        feature=feature, provider=provider, model_id=model_id,
                        status="ERROR", latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                        # A hard-budget rejection never reached a provider, so it
                        # must not look like billable input in the cost dashboard.
                        input_chars=0 if isinstance(exc, AIBudgetError) else input_chars,
                        output_chars=0, input_tokens=actual_input_tokens,
                        output_tokens=actual_output_tokens, error_type=type(exc).__name__,
                    )
                    raise
                actual_input_tokens, actual_output_tokens = _recorded_usage()
                await asyncio.to_thread(
                    _record,
                    reservation_id=reservation_id,
                    feature=feature, provider=provider, model_id=model_id,
                    status="SUCCESS", latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                    input_chars=input_chars, output_chars=_content_size(result),
                    input_tokens=actual_input_tokens, output_tokens=actual_output_tokens, error_type=None,
                )
                return result
            finally:
                _USAGE_CONTEXT.reset(usage_token)
        return wrapped
    return decorate
