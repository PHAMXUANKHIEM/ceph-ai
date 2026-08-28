"""Refresh the AI token-price snapshot used by the cost dashboard.

The public LiteLLM catalog is used as a regularly updated index. It carries
provider/source metadata and the normalized per-token rates; the snapshot
keeps the provider/model ids that ceph-ai records in telemetry so aliases
continue to resolve. A failed refresh never replaces the previous snapshot.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from shared import db
from shared.ai_cost import TOKEN_PRICES
from shared.ai_pricing import cache_path, load_cached_prices
from shared.models import AIInvocation

logger = logging.getLogger(__name__)

CATALOG_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
SOURCE_URLS = {
    "codex": "https://openai.com/api/pricing/",
    "claude": "https://platform.claude.com/docs/en/about-claude/pricing",
    "9router": "https://ai.google.dev/gemini-api/docs/pricing",
}


def fetch_catalog(url: str = CATALOG_URL, timeout: int = 20) -> dict:
    request = Request(url, headers={"User-Agent": "ceph-aiops-ai-pricing/1.0"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("pricing catalog is not a non-empty JSON object")
    return payload


def _candidate_keys(provider: str, model_id: str) -> list[str]:
    provider = (provider or "").strip().lower()
    model_id = (model_id or "").strip().lower()
    candidates = [model_id]
    for prefix in ("gc/", "gemini/", "openai/", "anthropic/", "claude/"):
        if model_id.startswith(prefix):
            candidates.append(model_id[len(prefix):])
    if provider == "claude" and model_id in {"default", "sonnet", "claude-sonnet-5-latest"}:
        candidates.extend(("claude-sonnet-5", "claude-sonnet-4-5"))
    if provider == "9router":
        candidates.extend(("gemini-2.5-flash", "gemini/gemini-2.5-flash"))
    return list(dict.fromkeys(candidates))


def _catalog_entry(catalog: dict, provider: str, model_id: str) -> tuple[str, dict] | None:
    lowered = {str(key).lower(): (str(key), value) for key, value in catalog.items()}
    for candidate in _candidate_keys(provider, model_id):
        item = lowered.get(candidate)
        if item and isinstance(item[1], dict):
            return item
    return None


def observed_models() -> list[tuple[str, str]]:
    try:
        with db.SessionLocal() as session:
            rows = session.query(AIInvocation.provider, AIInvocation.model_id).distinct().all()
        return [(str(provider), str(model_id)) for provider, model_id in rows]
    except Exception:
        logger.exception("Could not read observed AI models; refreshing built-in targets only")
        return []


def _price_record(provider: str, model_id: str, entry: dict, catalog_key: str, now: str) -> dict | None:
    try:
        input_rate = float(entry["input_cost_per_token"]) * 1_000_000
        output_rate = float(entry["output_cost_per_token"]) * 1_000_000
    except (KeyError, TypeError, ValueError):
        return None
    if input_rate < 0 or output_rate < 0:
        return None
    cached = entry.get("cache_read_input_token_cost")
    cached_rate = float(cached) * 1_000_000 if cached is not None else None
    if cached_rate is not None and cached_rate < 0:
        return None
    note = (
        f"Synced from LiteLLM catalog ({catalog_key}); verify account tier and proxy markup."
    )
    if provider == "9router":
        note = "Google list price synced from LiteLLM; 9router quota/markup may differ."
    return {
        "provider": provider,
        "model_id": model_id,
        "label": model_id,
        "input_usd_per_million_tokens": round(input_rate, 6),
        "output_usd_per_million_tokens": round(output_rate, 6),
        "cached_input_usd_per_million_tokens": round(cached_rate, 6) if cached_rate is not None else None,
        "source_url": str(entry.get("source") or SOURCE_URLS.get(provider, CATALOG_URL)),
        "as_of": now[:10],
        "note": note,
    }


def build_snapshot(catalog: dict, models: list[tuple[str, str]] | None = None, *, now: datetime | None = None) -> list[dict]:
    """Build records for built-in and currently observed provider/model pairs."""
    targets = {(price.provider, price.model_id) for price in TOKEN_PRICES}
    targets.update((provider.strip().lower(), model_id.strip()) for provider, model_id in (models or []))
    defaults = {(price.provider, price.model_id): price for price in TOKEN_PRICES}
    previous = {
        (str(item.get("provider", "")).lower(), str(item.get("model_id", "")).lower()): item
        for item in load_cached_prices()
    }
    as_of = (now or datetime.now(timezone.utc)).isoformat()
    records = []
    for provider, model_id in sorted(targets):
        match = _catalog_entry(catalog, provider, model_id)
        if match is None:
            old = previous.get((provider, model_id.lower()))
            if old is not None:
                records.append(old)
                logger.warning("No catalog price for %s/%s; keeping previous snapshot", provider, model_id)
            else:
                logger.warning("No catalog price for %s/%s; keeping built-in fallback", provider, model_id)
            continue
        record = _price_record(provider, model_id, match[1], match[0], as_of)
        if record is not None:
            default = defaults.get((provider, model_id))
            if default is not None:
                record["label"] = default.label
            records.append(record)
    if not records:
        raise ValueError("catalog did not contain a usable price for any configured model")
    return records


def write_snapshot(records: list[dict], path: Path | None = None, *, now: datetime | None = None) -> Path:
    destination = path or cache_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "source_url": CATALOG_URL,
        "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "prices": records,
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def refresh() -> Path:
    catalog = fetch_catalog()
    records = build_snapshot(catalog, observed_models())
    path = write_snapshot(records)
    logger.info("Updated %d AI prices in %s", len(records), path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="suppress informational output")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    try:
        refresh()
    except Exception:
        logger.exception("AI pricing refresh failed; existing cache was not replaced")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
