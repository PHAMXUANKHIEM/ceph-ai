"""Content-free AI usage telemetry and budget reservation.

All provider integrations can use :func:`ai_invocation` without persisting
prompt or response content.  The reservation is written before the provider
call and remains visible as ``RESERVED`` while the call is in flight, so a
hard budget does not blindly admit concurrent requests.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, text
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from shared import db
from shared.models import AIInvocation

logger = logging.getLogger(__name__)


class AIBudgetExceeded(RuntimeError):
    """Raised before a provider call when a configured hard budget is full."""


def estimate_tokens(text: str | None, *, chars_per_token: int | None = None) -> int:
    value = str(text or "")
    divisor = chars_per_token or max(1, int(settings.ai_cost_estimate_chars_per_token))
    return max(0, math.ceil(len(value) / divisor))


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * float(settings.ai_cost_input_usd_per_million_tokens) / 1_000_000
        + output_tokens * float(settings.ai_cost_output_usd_per_million_tokens) / 1_000_000
    )


def _window_start(now: datetime, monthly: bool) -> datetime:
    if monthly:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _spent_since(session, start: datetime) -> float:
    value = session.query(func.coalesce(func.sum(AIInvocation.estimated_cost_usd), 0.0)).filter(
        AIInvocation.created_at >= start,
        AIInvocation.status.in_(("RESERVED", "SUCCEEDED", "FAILED")),
    ).scalar()
    return float(value or 0.0)


def _budget_check(session, cost: float, now: datetime) -> None:
    daily = float(settings.ai_budget_daily_usd)
    monthly = float(settings.ai_budget_monthly_usd)
    if not settings.ai_budget_hard_limit or (daily <= 0 and monthly <= 0):
        return
    if (daily > 0 or monthly > 0) and not settings.ai_cost_input_usd_per_million_tokens and not settings.ai_cost_output_usd_per_million_tokens:
        raise AIBudgetExceeded(
            "AI hard budget đã bật nhưng chưa cấu hình giá token; từ chối gọi để tránh vượt ngân sách không đo được"
        )
    if daily > 0 and _spent_since(session, _window_start(now, monthly=False)) + cost > daily:
        raise AIBudgetExceeded("Đã đạt ngân sách AI trong ngày; yêu cầu bị chặn an toàn")
    if monthly > 0 and _spent_since(session, _window_start(now, monthly=True)) + cost > monthly:
        raise AIBudgetExceeded("Đã đạt ngân sách AI trong tháng; yêu cầu bị chặn an toàn")


def _lock_budget_reservation(session) -> None:
    """Serialize hard-budget reservations on the production database.

    The check and RESERVED-row insert happen in one transaction, but an
    aggregate SUM alone does not lock a row. PostgreSQL's transaction-scoped
    advisory lock closes that check-then-insert race across Dashboard,
    Watcher, Worker and Code Repair processes. SQLite remains supported for
    tests/dev, but production rejects SQLite elsewhere in the configuration
    gates and therefore cannot rely on this lock there.
    """
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('ceph-ai:ai-budget'))"))


def _usage_from_response(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None, None

    def read(name: str) -> int | None:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    return read("prompt_tokens"), read("completion_tokens")


class _AIInvocation(AbstractContextManager):
    def __init__(
        self,
        *,
        feature: str,
        provider: str,
        model: str | None,
        input_text: str | None = None,
        input_chars: int | None = None,
        max_output_tokens: int = 0,
        cluster_id: str | None = None,
        actor: str | None = None,
        retry_count: int = 0,
        fallback_used: bool = False,
    ) -> None:
        self.feature = feature[:64]
        self.provider = provider[:32]
        self.model = (model or "unknown")[:128]
        self.input_tokens = estimate_tokens(input_text) if input_chars is None else math.ceil(max(0, input_chars) / max(1, int(settings.ai_cost_estimate_chars_per_token)))
        self.max_output_tokens = max(0, int(max_output_tokens))
        self.cluster_id = cluster_id
        self.actor = actor
        self.retry_count = max(0, int(retry_count))
        self.fallback_used = bool(fallback_used)
        self.row_id: str | None = None
        self.started = 0.0
        self.response: Any = None
        self.output_text: str | None = None

    def __enter__(self):
        if not settings.ai_telemetry_enabled:
            return self
        self.started = time.monotonic()
        now = datetime.utcnow()
        reserved_cost = _estimate_cost(self.input_tokens, self.max_output_tokens)
        try:
            with db.SessionLocal() as session:
                _lock_budget_reservation(session)
                _budget_check(session, reserved_cost, now)
                row = AIInvocation(
                    id=str(uuid.uuid4()),
                    feature=self.feature,
                    provider=self.provider,
                    model=self.model,
                    status="RESERVED",
                    input_tokens=self.input_tokens,
                    output_tokens=self.max_output_tokens,
                    tokens_estimated=True,
                    estimated_cost_usd=reserved_cost,
                    retry_count=self.retry_count,
                    fallback_used=self.fallback_used,
                    cluster_id=self.cluster_id,
                    actor=self.actor,
                    created_at=now,
                )
                session.add(row)
                session.commit()
                self.row_id = row.id
        except AIBudgetExceeded:
            raise
        except SQLAlchemyError:
            # Telemetry must not take down an otherwise healthy provider call
            # during a rolling deployment before the migration has completed.
            # Hard-budget errors are deliberately re-raised above.
            logger.warning("AI telemetry storage unavailable; continuing without ledger row")
        return self

    def set_response(self, response: Any = None, *, output_text: str | None = None) -> None:
        self.response = response
        self.output_text = output_text

    def __exit__(self, exc_type, exc, _tb):
        if not settings.ai_telemetry_enabled or self.row_id is None:
            return False
        input_tokens, output_tokens = _usage_from_response(self.response)
        if input_tokens is None:
            input_tokens = self.input_tokens
        if output_tokens is None:
            output_tokens = estimate_tokens(self.output_text)
        actual_cost = _estimate_cost(input_tokens, output_tokens)
        status = "FAILED" if exc_type is not None else "SUCCEEDED"
        error_code = exc_type.__name__[:64] if exc_type is not None else None
        try:
            with db.SessionLocal() as session:
                row = session.get(AIInvocation, self.row_id)
                if row is not None:
                    row.status = status
                    row.input_tokens = input_tokens
                    row.output_tokens = output_tokens
                    row.tokens_estimated = _usage_from_response(self.response) == (None, None)
                    row.estimated_cost_usd = actual_cost
                    row.duration_ms = max(0, int((time.monotonic() - self.started) * 1000))
                    row.error_code = error_code
                    session.commit()
        except Exception:
            logger.exception("AI telemetry write failed for feature=%s", self.feature)
        return False


def ai_invocation(**kwargs) -> _AIInvocation:
    """Create a telemetry/budget context around one provider call."""
    return _AIInvocation(**kwargs)


def summary(period_hours: int, *, now: datetime | None = None) -> dict:
    """Return content-free usage grouped by feature/provider/model."""
    now = now or datetime.utcnow()
    start = now - timedelta(hours=max(1, int(period_hours)))
    try:
        with db.SessionLocal() as session:
            grouped = session.query(
                AIInvocation.feature,
                AIInvocation.provider,
                AIInvocation.model,
                func.count(AIInvocation.id),
                func.sum(case((AIInvocation.status == "FAILED", 1), else_=0)),
                func.coalesce(func.sum(AIInvocation.input_tokens), 0),
                func.coalesce(func.sum(AIInvocation.output_tokens), 0),
                func.coalesce(func.sum(AIInvocation.estimated_cost_usd), 0.0),
            ).filter(AIInvocation.created_at >= start).group_by(
                AIInvocation.feature, AIInvocation.provider, AIInvocation.model
            ).all()
    except SQLAlchemyError:
        logger.warning("AI telemetry storage unavailable; returning empty summary")
        grouped = []
    groups = [
        {
            "feature": feature,
            "provider": provider,
            "model": model,
            "calls": int(calls or 0),
            "errors": int(errors or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "estimated_cost_usd": float(cost or 0.0),
        }
        for feature, provider, model, calls, errors, input_tokens, output_tokens, cost in grouped
    ]
    return {
        "calls": sum(item["calls"] for item in groups),
        "errors": sum(item["errors"] for item in groups),
        "input_tokens": sum(item["input_tokens"] for item in groups),
        "output_tokens": sum(item["output_tokens"] for item in groups),
        "estimated_cost_usd": sum(item["estimated_cost_usd"] for item in groups),
        "groups": groups,
    }
