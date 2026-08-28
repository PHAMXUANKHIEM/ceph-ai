"""Content-free AI budget checks and atomic hard-budget reservations."""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime

from config.settings import settings
from shared import db
from shared.models import AIBudgetLock, AIInvocation

logger = logging.getLogger(__name__)


class AIBudgetError(RuntimeError):
    """Base class for operator-visible budget guard failures."""


class AIBudgetExceededError(AIBudgetError):
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


class AIBudgetUnpricedError(AIBudgetError):
    """Raised in hard mode when a model has no defensible token price."""

    def __init__(self, provider: str, model_id: str, *, reason: str | None = None):
        super().__init__(
            reason or f"Không thể gọi AI model {provider}/{model_id}: chưa có đơn giá để kiểm soát budget"
        )


class AIBudgetConfigurationError(AIBudgetError):
    """Raised when the budget lock table has not been migrated correctly."""


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


def _spent_since(session, start: datetime) -> tuple[float, int]:
    rows = session.query(AIInvocation).filter(AIInvocation.created_at >= start).all()
    spent = 0.0
    unpriced = 0
    for row in rows:
        # A rejected call never reached a provider and must not poison future
        # hard-budget checks as an unknown-priced historical invocation.
        if row.error_type in {"AIBudgetUnpricedError", "AIBudgetConfigurationError"}:
            continue
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
    with db.SessionLocal() as session:
        daily_spent, daily_unpriced = _spent_since(session, _period_start(now, "daily"))
        monthly_spent, monthly_unpriced = _spent_since(session, _period_start(now, "monthly"))

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


def _reserve_hard_budget(
    provider: str, model_id: str, input_chars: int, output_tokens: int,
    *, now: datetime, daily_limit: float, monthly_limit: float,
    estimated_call: float,
) -> str:
    """Reserve one invocation while holding stable per-period DB locks.

    The RESERVED AIInvocation is the accounting row. The observer updates it
    to SUCCESS/ERROR after the provider returns, so a concurrent request sees
    the reservation and cannot spend the same remaining budget twice.
    """
    active_periods = [
        ("daily", daily_limit, _period_start(now, "daily")),
        ("monthly", monthly_limit, _period_start(now, "monthly")),
    ]
    active_periods = [item for item in active_periods if item[1]]
    reservation_id = str(uuid.uuid4())
    try:
        with db.SessionLocal() as session:
            lock_rows = (
                session.query(AIBudgetLock)
                .filter(AIBudgetLock.period.in_([item[0] for item in active_periods]))
                .order_by(AIBudgetLock.period)
                .with_for_update()
                .all()
            )
            if len(lock_rows) != len(active_periods):
                raise AIBudgetConfigurationError(
                    "Bảng khóa AI Budget chưa được migrate đầy đủ; chưa gọi provider"
                )
            lock_by_period = {row.period: row for row in lock_rows}
            for period_name, limit, period_start in active_periods:
                lock_row = lock_by_period[period_name]
                if lock_row.period_start != period_start:
                    lock_row.period_start = period_start
                    lock_row.updated_at = now
                spent, unpriced = _spent_since(session, period_start)
                if unpriced:
                    raise AIBudgetUnpricedError(
                        provider, model_id,
                        reason=f"AI budget {period_name} có {unpriced} lượt chưa có đơn giá; "
                               "chưa gọi provider",
                    )
                projected = spent + estimated_call
                if projected >= limit:
                    raise AIBudgetExceededError(period_name, spent, limit, estimated_call)
            session.add(AIInvocation(
                id=reservation_id, feature="__budget_reservation__",
                provider=provider, model_id=model_id, status="RESERVED", latency_ms=0,
                input_chars=max(0, int(input_chars or 0)), output_chars=max(0, output_tokens) * 4,
                error_type=None, created_at=now,
            ))
            session.commit()
    except AIBudgetError:
        raise
    except Exception as exc:
        logger.exception("AI budget reservation failed closed")
        raise AIBudgetConfigurationError(
            "Không thể kiểm tra khóa AI Budget; chưa gọi provider"
        ) from exc
    return reservation_id


def check(
    provider: str, model_id: str, input_chars: int, *, now: datetime | None = None,
) -> str | None:
    """Warn at 80%; in hard mode atomically reserve or reject the call."""
    daily_limit, monthly_limit = _budget_limits()
    if not (daily_limit or monthly_limit):
        return None
    now = now or datetime.utcnow()
    reserve_output = max(0, int(getattr(settings, "ai_cost_budget_reserve_output_tokens", 2048)))
    estimated_call = _estimate_cost(provider, model_id, input_chars, reserve_output * 4)
    if estimated_call is None:
        logger.warning("AI budget cannot price provider=%s model=%s", provider, model_id)
        if bool(getattr(settings, "ai_cost_budget_hard_limit", False)):
            raise AIBudgetUnpricedError(provider, model_id)
        return None

    if not bool(getattr(settings, "ai_cost_budget_hard_limit", False)):
        for period_name, limit in (("daily", daily_limit), ("monthly", monthly_limit)):
            if not limit:
                continue
            try:
                with db.SessionLocal() as session:
                    spent, _ = _spent_since(session, _period_start(now, period_name))
            except Exception:
                # Soft mode is advisory: a temporary telemetry DB outage must
                # not make an otherwise configured AI provider unavailable.
                logger.exception("AI budget status unavailable in soft mode")
                continue
            projected = spent + estimated_call
            if projected >= limit * 0.8:
                logger.warning(
                    "AI budget %s at %.1f%%: spent=$%.6f projected=$%.6f limit=$%.6f",
                    period_name, projected / limit * 100, spent, projected, limit,
                )
        return None

    return _reserve_hard_budget(
        provider, model_id, input_chars, reserve_output, now=now,
        daily_limit=daily_limit, monthly_limit=monthly_limit, estimated_call=estimated_call,
    )
