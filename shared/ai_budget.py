"""Content-free AI budget checks based on recorded invocation estimates."""

from __future__ import annotations

import math
import logging
from datetime import datetime

from config.settings import settings
from shared import db
from shared.models import AIInvocation

logger = logging.getLogger(__name__)


class AIBudgetExceededError(Exception):
    """Raised when the configured hard AI budget would be exceeded."""

    def __init__(self, period: str, spent: float, limit: float, estimated_call: float):
        self.period = period
        self.spent = spent
        self.limit = limit
        self.estimated_call = estimated_call
        super().__init__(
            f"AI budget {period} đã đạt giới hạn ${limit:.4f} "
            f"(đã dùng ${spent:.4f}, lượt này ước tính ${estimated_call:.4f})"
        )


def _budget_limits() -> tuple[float, float]:
    return (
        max(0.0, float(getattr(settings, "ai_cost_daily_budget_usd", 0.0))),
        max(0.0, float(getattr(settings, "ai_cost_monthly_budget_usd", 0.0))),
    )


def _estimate_cost(provider: str, model_id: str, input_chars: int, output_chars: int) -> float | None:
    # Lazy import keeps the price table independent from the guard module.
    from shared.ai_cost import CHARS_PER_TOKEN, _configured_fallback, _resolve_price

    price = _resolve_price(provider, model_id) or _configured_fallback()
    if price is None:
        return None
    input_tokens = math.ceil(max(0, int(input_chars or 0)) / CHARS_PER_TOKEN)
    output_tokens = math.ceil(max(0, int(output_chars or 0)) / CHARS_PER_TOKEN)
    return (input_tokens * price.input_usd_per_million_tokens
            + output_tokens * price.output_usd_per_million_tokens) / 1_000_000


def _period_start(now: datetime, period: str) -> datetime:
    if period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _spent_since(start: datetime) -> tuple[float, int]:
    with db.SessionLocal() as session:
        rows = session.query(AIInvocation).filter(AIInvocation.created_at >= start).all()
    spent = 0.0
    unpriced = 0
    for row in rows:
        cost = _estimate_cost(row.provider, row.model_id, row.input_chars, row.output_chars)
        if cost is None:
            unpriced += 1
        else:
            spent += cost
    return spent, unpriced


def status(*, now: datetime | None = None) -> dict:
    """Return current daily/monthly budget status without reading content."""
    now = now or datetime.utcnow()
    daily_limit, monthly_limit = _budget_limits()
    daily_spent, daily_unpriced = _spent_since(_period_start(now, "daily"))
    monthly_spent, monthly_unpriced = _spent_since(_period_start(now, "monthly"))

    def period(spent: float, limit: float) -> dict:
        percent = (spent / limit * 100) if limit else None
        return {
            "limit_usd": round(limit, 6),
            "spent_usd": round(spent, 6),
            "remaining_usd": round(max(0.0, limit - spent), 6) if limit else None,
            "percent": round(percent, 1) if percent is not None else None,
        }

    return {
        "enabled": bool(daily_limit or monthly_limit),
        "hard_limit": bool(getattr(settings, "ai_cost_budget_hard_limit", False)),
        "timezone": "UTC",
        "daily": period(daily_spent, daily_limit),
        "monthly": period(monthly_spent, monthly_limit),
        "unpriced_calls": max(daily_unpriced, monthly_unpriced),
    }


def check(provider: str, model_id: str, input_chars: int, *, now: datetime | None = None) -> None:
    """Warn at 80% and optionally block a call before it reaches a provider."""
    daily_limit, monthly_limit = _budget_limits()
    if not (daily_limit or monthly_limit):
        return
    now = now or datetime.utcnow()
    reserve_output = max(0, int(getattr(settings, "ai_cost_budget_reserve_output_tokens", 2048)))
    estimated_call = _estimate_cost(provider, model_id, input_chars, reserve_output * 4)
    if estimated_call is None:
        logger.warning("AI budget cannot price provider=%s model=%s", provider, model_id)
        return

    for period_name, limit in (("daily", daily_limit), ("monthly", monthly_limit)):
        if not limit:
            continue
        spent, _ = _spent_since(_period_start(now, period_name))
        projected = spent + estimated_call
        if projected >= limit * 0.8:
            logger.warning(
                "AI budget %s at %.1f%%: spent=$%.6f projected=$%.6f limit=$%.6f",
                period_name, projected / limit * 100, spent, projected, limit,
            )
        if projected >= limit and bool(getattr(settings, "ai_cost_budget_hard_limit", False)):
            raise AIBudgetExceededError(period_name, spent, limit, estimated_call)
