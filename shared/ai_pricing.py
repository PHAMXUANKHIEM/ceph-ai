"""Load the last validated runtime AI pricing snapshot.

The updater writes this cache atomically. Cost calculation remains available
when the provider catalog is temporarily unreachable by falling back to the
versioned prices in :mod:`shared.ai_cost`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable

from config.settings import settings

_PRICE_FIELDS = {
    "provider",
    "model_id",
    "label",
    "input_usd_per_million_tokens",
    "output_usd_per_million_tokens",
    "cached_input_usd_per_million_tokens",
    "source_url",
    "as_of",
    "note",
}


def cache_path() -> Path:
    configured = str(getattr(settings, "ai_cost_pricing_cache_path", ".ai-pricing.json") or ".ai-pricing.json")
    path = Path(configured).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _valid_price(value: object) -> bool:
    if not isinstance(value, dict) or not {"provider", "model_id"}.issubset(value):
        return False
    if not str(value.get("provider") or "").strip() or not str(value.get("model_id") or "").strip():
        return False
    for field in ("input_usd_per_million_tokens", "output_usd_per_million_tokens"):
        try:
            number = float(value[field])
        except (KeyError, TypeError, ValueError):
            return False
        if number < 0 or not math.isfinite(number):
            return False
    cached = value.get("cached_input_usd_per_million_tokens")
    if cached is not None:
        try:
            number = float(cached)
        except (TypeError, ValueError):
            return False
        if number < 0 or not math.isfinite(number):
            return False
    return True


def load_cached_prices() -> list[dict]:
    """Return only schema-valid records from the updater's cache."""
    try:
        payload = json.loads(cache_path().read_text(encoding="utf-8"))
        records = payload.get("prices") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            return []
        result = []
        for raw in records:
            if not _valid_price(raw):
                continue
            result.append({
                "provider": str(raw["provider"]).strip(),
                "model_id": str(raw["model_id"]).strip(),
                "label": str(raw.get("label") or raw["model_id"]),
                "input_usd_per_million_tokens": float(raw["input_usd_per_million_tokens"]),
                "output_usd_per_million_tokens": float(raw["output_usd_per_million_tokens"]),
                "cached_input_usd_per_million_tokens": (
                    float(raw["cached_input_usd_per_million_tokens"])
                    if raw.get("cached_input_usd_per_million_tokens") is not None else None
                ),
                "source_url": str(raw.get("source_url") or ""),
                "as_of": str(raw.get("as_of") or "runtime"),
                "note": str(raw.get("note") or "Runtime pricing snapshot."),
            })
        return result
    except (OSError, ValueError, TypeError):
        return []


def merge_with_defaults(default_prices: Iterable[object]) -> list[object]:
    """Overlay cached records by provider/model while retaining safe defaults."""
    defaults = tuple(default_prices)
    cached = {
        (str(item.get("provider", "")).lower(), str(item.get("model_id", "")).lower()): item
        for item in load_cached_prices()
    }
    merged = []
    seen = set()
    for default in defaults:
        key = (default.provider.lower(), default.model_id.lower())
        raw = cached.get(key)
        if raw is None:
            merged.append(default)
        else:
            merged.append(type(default)(**{
                "provider": raw.get("provider", default.provider),
                "model_id": raw.get("model_id", default.model_id),
                "label": raw.get("label", default.label),
                "input_usd_per_million_tokens": raw["input_usd_per_million_tokens"],
                "output_usd_per_million_tokens": raw["output_usd_per_million_tokens"],
                "cached_input_usd_per_million_tokens": raw.get(
                    "cached_input_usd_per_million_tokens", default.cached_input_usd_per_million_tokens
                ),
                "source_url": raw.get("source_url", default.source_url),
                "as_of": raw.get("as_of", default.as_of),
                "note": raw.get("note", default.note),
            }))
        seen.add(key)
    for key, raw in cached.items():
        if key not in seen:
            # Keep cache-only models available for newly observed telemetry.
            if defaults:
                merged.append(type(defaults[0])(**raw))
    return merged
