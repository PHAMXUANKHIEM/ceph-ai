"""Read-only AI invocation volume and model-specific cost estimates."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from dataclasses import asdict, dataclass

from config.settings import settings
from shared import db
from shared.models import AIInvocation

CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class TokenPrice:
    """Public list price used for an estimate, not a provider invoice."""

    provider: str
    model_id: str
    label: str
    input_usd_per_million_tokens: float
    output_usd_per_million_tokens: float
    cached_input_usd_per_million_tokens: float | None
    source_url: str
    as_of: str
    note: str


# Keep this table limited to models currently observed by ceph-ai.  Aliases
# and proxy prefixes are resolved below so the telemetry can still be priced
# when a provider exposes a qualified model id.
TOKEN_PRICES: tuple[TokenPrice, ...] = (
    TokenPrice(
        provider="codex", model_id="gpt-5.6-sol", label="GPT-5.6 Sol",
        input_usd_per_million_tokens=4.00,
        output_usd_per_million_tokens=20.00,
        cached_input_usd_per_million_tokens=0.40,
        source_url="https://help.openai.com/en/articles/20001415-chatgpt-rate-card-enterprise-token-based-pricing",
        as_of="2026-08-28",
        note="Codex/ChatGPT rate-card estimate; included subscription quota is not a separate API invoice.",
    ),
    TokenPrice(
        provider="claude", model_id="sonnet", label="Claude Sonnet 5 (CLI alias)",
        input_usd_per_million_tokens=2.00,
        output_usd_per_million_tokens=10.00,
        cached_input_usd_per_million_tokens=None,
        source_url="https://www.anthropic.com/claude/sonnet",
        as_of="2026-08-28",
        note="Current Sonnet CLI alias mapped to Sonnet 5; subscription usage may be quota-based.",
    ),
    TokenPrice(
        provider="9router", model_id="gc/gemini-2.5-flash", label="Gemini 2.5 Flash via 9router",
        input_usd_per_million_tokens=0.30,
        output_usd_per_million_tokens=2.50,
        cached_input_usd_per_million_tokens=0.03,
        source_url="https://ai.google.dev/gemini-api/docs/pricing",
        as_of="2026-08-28",
        note="Google direct list price used as a proxy estimate; 9router tier, quota, or markup may differ.",
    ),
)


def pricing_table() -> list[dict]:
    """Return the auditable price table for the dashboard/API."""
    return [asdict(price) for price in TOKEN_PRICES]


def _resolve_price(provider: str, model_id: str) -> TokenPrice | None:
    provider_key = (provider or "").strip().lower()
    model_key = (model_id or "").strip().lower()
    for price in TOKEN_PRICES:
        if price.provider == provider_key and price.model_id.lower() == model_key:
            return price

    # Claude Code reports the stable family alias as simply ``sonnet``.
    if provider_key == "claude" and model_key in {"default", "claude-sonnet-5", "claude-sonnet-5-latest"}:
        return next(price for price in TOKEN_PRICES if price.provider == "claude")

    # 9router qualifies Google Cloud models with ``gc/`` in its model id.
    if provider_key == "9router" and model_key.removeprefix("gc/") == "gemini-2.5-flash":
        return next(price for price in TOKEN_PRICES if price.provider == "9router")
    return None


def _configured_fallback() -> TokenPrice | None:
    input_rate = max(0.0, float(getattr(settings, "ai_cost_input_usd_per_million_tokens", 0.0)))
    output_rate = max(0.0, float(getattr(settings, "ai_cost_output_usd_per_million_tokens", 0.0)))
    if not (input_rate or output_rate):
        return None
    return TokenPrice(
        provider="*", model_id="*", label="Configured fallback",
        input_usd_per_million_tokens=input_rate,
        output_usd_per_million_tokens=output_rate,
        cached_input_usd_per_million_tokens=None,
        source_url="",
        as_of="runtime",
        note="Configured via AI_COST_* settings; verify this rate against the provider.",
    )


def _usd_to_vnd_rate() -> float:
    return max(0.0, float(getattr(settings, "ai_cost_usd_to_vnd", 26290.0)))


def _estimated_tokens(chars: int) -> int:
    return math.ceil(max(0, int(chars or 0)) / CHARS_PER_TOKEN)


def _optimization_summary(groups: list[dict], total_cost: float, hours: int, usd_to_vnd: float) -> dict:
    """Suggest cheaper priced models without changing the active provider.

    Suggestions are advisory: providers may be unavailable or have different
    quotas/markups. The calculation uses the same input/output token estimate
    as the cost table, so an operator can audit every number on this page.
    """
    recommendations = []
    for row in groups:
        input_tokens = row["input_tokens"]
        output_tokens = row["output_tokens"]
        current = row["estimated_cost_usd"]
        candidates = []
        for price in TOKEN_PRICES:
            if price.provider == row["provider"] and price.model_id.lower() == row["model_id"].lower():
                continue
            candidate_cost = (
                input_tokens * price.input_usd_per_million_tokens
                + output_tokens * price.output_usd_per_million_tokens
            ) / 1_000_000
            candidates.append((candidate_cost, price))
        if not candidates:
            continue
        candidate_cost, price = min(candidates, key=lambda item: item[0])
        baseline = current if current is not None else candidate_cost
        if baseline <= 0 or candidate_cost >= baseline:
            continue
        savings = baseline - candidate_cost
        recommendations.append({
            "feature": row["feature"],
            "current_provider": row["provider"],
            "current_model_id": row["model_id"],
            "recommended_provider": price.provider,
            "recommended_model_id": price.model_id,
            "recommended_label": price.label,
            "current_cost_usd": round(current, 6) if current is not None else None,
            "recommended_cost_usd": round(candidate_cost, 6),
            "estimated_savings_usd": round(savings, 6),
            "estimated_savings_vnd": round(savings * usd_to_vnd),
            "savings_percent": round(savings / baseline * 100, 1),
        })
    recommendations.sort(key=lambda item: item["estimated_savings_usd"], reverse=True)
    monthly_cost = total_cost / hours * 730 if total_cost else 0.0
    return {
        "monthly_projection_usd": round(monthly_cost, 6),
        "monthly_projection_vnd": round(monthly_cost * usd_to_vnd),
        "recommendations": recommendations[:10],
    }


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
    fallback = _configured_fallback()
    usd_to_vnd = _usd_to_vnd_rate()
    result = []
    total_cost = 0.0
    priced_groups = 0
    unpriced_groups = 0
    rates: set[tuple[float, float]] = set()
    for (feature, provider, model_id), items in sorted(groups.items()):
        input_tokens = sum(_estimated_tokens(row.input_chars) for row in items)
        output_tokens = sum(_estimated_tokens(row.output_chars) for row in items)
        price = _resolve_price(provider, model_id) or fallback
        if price is None:
            cost = None
            unpriced_groups += 1
            input_rate = output_rate = None
            source = None
            note = "Chưa có đơn giá cho model này."
        else:
            input_rate = price.input_usd_per_million_tokens
            output_rate = price.output_usd_per_million_tokens
            cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
            total_cost += cost
            priced_groups += 1
            rates.add((input_rate, output_rate))
            source = price.source_url or "settings"
            note = price.note
        result.append({
            "feature": feature,
            "provider": provider,
            "model_id": model_id,
            "calls": len(items),
            "errors": sum(row.status == "ERROR" for row in items),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_usd_per_million_tokens": input_rate,
            "output_usd_per_million_tokens": output_rate,
            "pricing_source": source,
            "pricing_note": note,
            "estimated_cost_usd": round(cost, 6) if cost is not None else None,
            "estimated_cost_vnd": round(cost * usd_to_vnd) if cost is not None else None,
        })
    common_rates = next(iter(rates)) if len(rates) == 1 else (None, None)
    optimization = _optimization_summary(result, total_cost, hours, usd_to_vnd)
    return {
        "hours": hours,
        "observed_at": now.isoformat() + "Z",
        "pricing_configured": priced_groups > 0,
        "pricing_complete": bool(groups) and unpriced_groups == 0,
        "unpriced_groups": unpriced_groups,
        "input_usd_per_million_tokens": common_rates[0],
        "output_usd_per_million_tokens": common_rates[1],
        "calls": len(rows),
        "errors": sum(row.status == "ERROR" for row in rows),
        "input_tokens": sum(row["input_tokens"] for row in result),
        "output_tokens": sum(row["output_tokens"] for row in result),
        "estimated_cost_usd": round(total_cost, 6) if priced_groups else None,
        "usd_to_vnd": usd_to_vnd,
        "estimated_cost_vnd": round(total_cost * usd_to_vnd) if priced_groups else None,
        "optimization": optimization,
        "pricing_table": pricing_table(),
        "groups": result,
    }
