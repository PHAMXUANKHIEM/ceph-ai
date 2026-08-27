"""Read-only AI invocation volume and configurable cost estimates."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta

from config.settings import settings
from shared import db
from shared.models import AIInvocation

CHARS_PER_TOKEN = 4


def _estimated_tokens(chars: int) -> int:
    return math.ceil(max(0, int(chars or 0)) / CHARS_PER_TOKEN)


def summary(hours: int = 24, *, now: datetime | None = None) -> dict:
    """Aggregate content-free telemetry; never reads prompt/response content."""
    hours = max(1, min(int(hours), 8760))
    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=hours)
    with db.SessionLocal() as session:
        rows = session.query(AIInvocation).filter(AIInvocation.created_at >= cutoff).all()

    groups: dict[tuple[str, str, str], list[AIInvocation]] = defaultdict(list)
    for row in rows:
        groups[(row.feature, row.provider, row.model_id)].append(row)
    input_rate = max(0.0, float(getattr(settings, "ai_cost_input_usd_per_million_tokens", 0.0)))
    output_rate = max(0.0, float(getattr(settings, "ai_cost_output_usd_per_million_tokens", 0.0)))
    configured = bool(input_rate or output_rate)
    result = []
    total_cost = 0.0
    for (feature, provider, model_id), items in sorted(groups.items()):
        input_tokens = sum(_estimated_tokens(row.input_chars) for row in items)
        output_tokens = sum(_estimated_tokens(row.output_chars) for row in items)
        cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
        total_cost += cost
        result.append({
            "feature": feature,
            "provider": provider,
            "model_id": model_id,
            "calls": len(items),
            "errors": sum(row.status == "ERROR" for row in items),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(cost, 6) if configured else None,
        })
    return {
        "hours": hours,
        "observed_at": now.isoformat() + "Z",
        "pricing_configured": configured,
        "input_usd_per_million_tokens": input_rate,
        "output_usd_per_million_tokens": output_rate,
        "calls": len(rows),
        "errors": sum(row.status == "ERROR" for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in result),
        "output_tokens": sum(row["output_tokens"] for row in result),
        "estimated_cost_usd": round(total_cost, 6) if configured else None,
        "groups": result,
    }
