"""Safe, opt-in model routing for AI cost experiments.

Routing is deliberately conservative. The default mode is ``advisory`` and
never changes a provider request. ``canary`` may select a cheaper model only
when it is priced, belongs to the same provider, is explicitly allowlisted,
meets the configured saving threshold, and falls inside the deterministic
canary percentage. This keeps a bad model choice reversible: turn the mode
back to ``advisory`` or ``off`` without changing any provider credentials.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from config.settings import settings
from shared.ai_cost import _active_prices, _resolve_price

logger = logging.getLogger(__name__)

VALID_MODES = {"off", "advisory", "canary"}


def _features() -> set[str]:
    raw = str(getattr(settings, "ai_cost_routing_features", "") or "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _allowlisted_models() -> set[tuple[str, str]]:
    """Return provider/model pairs explicitly verified by an operator."""
    raw = str(getattr(settings, "ai_cost_routing_model_allowlist", "") or "")
    result: set[tuple[str, str]] = set()
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        separator = ":" if ":" in value else "/"
        provider_name, model_name = (part.strip().lower() for part in value.split(separator, 1))
        if provider_name and model_name:
            result.add((provider_name, model_name))
    return result


def _estimated_cost(price: Any, input_chars: int, output_tokens: int) -> float:
    # The same conservative 4 chars/token estimate is used by the cost and
    # budget layers. This function returns only a number; it never retains
    # prompt or response content.
    input_tokens = max(0, (int(input_chars or 0) + 3) // 4)
    output_tokens = max(0, int(output_tokens or 0))
    return (
        input_tokens * price.input_usd_per_million_tokens
        + output_tokens * price.output_usd_per_million_tokens
    ) / 1_000_000


def choose_model(
    feature: str,
    provider: str,
    model_id: str,
    *,
    input_chars: int = 0,
    output_tokens: int = 0,
    request_id: str = "",
) -> dict[str, Any]:
    """Return an auditable route decision without changing settings.

    ``selected_model`` equals ``model_id`` unless an enabled canary is
    eligible. Callers should pass the selected value to the provider adapter
    and record the actual adapter choice through ``mark_ai_provider``.
    """
    provider = (provider or "").strip().lower()
    model_id = (model_id or "default").strip() or "default"
    mode = str(getattr(settings, "ai_cost_routing_mode", "advisory") or "advisory").strip().lower()
    if mode not in VALID_MODES:
        logger.warning("Unknown AI cost routing mode %r; using advisory", mode)
        mode = "advisory"
    result: dict[str, Any] = {
        "feature": feature,
        "provider": provider,
        "current_model": model_id,
        "selected_model": model_id,
        "recommended_model": None,
        "mode": mode,
        "canary": False,
        "savings_percent": None,
        "reason": "routing disabled" if mode == "off" else "advisory only",
    }
    if mode == "off" or feature not in _features():
        return result

    current = _resolve_price(provider, model_id)
    if current is None:
        result["reason"] = "current model has no validated price"
        return result
    # Current callers select a model inside an already selected provider
    # adapter. Cross-provider routing would send (for example) a Claude model
    # through the Codex adapter, so reject it until provider switching exists.
    if not bool(getattr(settings, "ai_cost_routing_same_provider_only", True)):
        logger.warning("Cross-provider AI routing requested but is unsupported; keeping same-provider routing")
    candidates = []
    for price in _active_prices():
        if price.provider.lower() == provider and price.model_id.lower() == model_id.lower():
            continue
        if price.provider.lower() != provider:
            continue
        candidate_cost = _estimated_cost(price, input_chars, output_tokens)
        current_cost = _estimated_cost(current, input_chars, output_tokens)
        if candidate_cost >= current_cost or current_cost <= 0:
            continue
        savings_percent = (current_cost - candidate_cost) / current_cost * 100
        candidates.append((savings_percent, candidate_cost, price))
    if not candidates:
        result["reason"] = "no cheaper validated model for this provider"
        return result

    savings_percent, candidate_cost, candidate = max(candidates, key=lambda item: item[0])
    result["recommended_model"] = candidate.model_id
    result["recommended_provider"] = candidate.provider
    result["savings_percent"] = round(savings_percent, 2)
    result["recommended_cost_usd"] = round(candidate_cost, 8)
    minimum = max(0.0, float(getattr(settings, "ai_cost_routing_min_savings_percent", 15.0)))
    if savings_percent < minimum:
        result["reason"] = "cheaper model is below the minimum saving threshold"
        return result

    percent = max(0, min(100, int(getattr(settings, "ai_cost_routing_canary_percent", 0))))
    if mode != "canary" or percent <= 0:
        result["reason"] = "cheaper model found; canary is not enabled"
        return result
    allowlist = _allowlisted_models()
    candidate_key = (candidate.provider.lower(), candidate.model_id.lower())
    if not allowlist:
        result["reason"] = "canary requires an explicit verified model allowlist"
        return result
    if candidate_key not in allowlist:
        result["reason"] = "candidate is not in the explicit verified model allowlist"
        return result
    bucket = int(hashlib.sha256(f"{feature}:{request_id}".encode()).hexdigest()[:8], 16) % 100
    if bucket >= percent:
        result["reason"] = "request is outside the canary percentage"
        return result
    result["selected_model"] = candidate.model_id
    result["canary"] = True
    result["reason"] = "same-provider canary selected"
    return result
