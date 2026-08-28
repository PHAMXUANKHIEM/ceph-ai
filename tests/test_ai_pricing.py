from datetime import datetime, timezone

from config.settings import settings
from scripts.update_ai_pricing import build_snapshot, write_snapshot
from shared.ai_cost import pricing_table


def test_build_snapshot_maps_aliases_and_normalizes_rates():
    catalog = {
        "gpt-5.6-sol": {
            "input_cost_per_token": 0.000004,
            "output_cost_per_token": 0.00002,
            "cache_read_input_token_cost": 0.0000004,
            "source": "https://openai.com/api/pricing/",
        },
        "claude-sonnet-5": {
            "input_cost_per_token": 0.000002,
            "output_cost_per_token": 0.00001,
        },
        "gemini-2.5-flash": {
            "input_cost_per_token": 0.0000003,
            "output_cost_per_token": 0.0000025,
            "cache_read_input_token_cost": 0.00000003,
        },
    }

    records = build_snapshot(catalog, now=datetime(2026, 8, 28, tzinfo=timezone.utc))
    by_key = {(row["provider"], row["model_id"]): row for row in records}
    assert by_key[("codex", "gpt-5.6-sol")]["input_usd_per_million_tokens"] == 4.0
    assert by_key[("claude", "sonnet")]["output_usd_per_million_tokens"] == 10.0
    assert by_key[("9router", "gc/gemini-2.5-flash")]["cached_input_usd_per_million_tokens"] == 0.03
    assert by_key[("claude", "sonnet")]["as_of"] == "2026-08-28"


def test_cost_table_uses_validated_runtime_snapshot(tmp_path, monkeypatch):
    destination = tmp_path / "pricing.json"
    write_snapshot([{
        "provider": "codex",
        "model_id": "gpt-5.6-sol",
        "label": "Updated GPT",
        "input_usd_per_million_tokens": 9.0,
        "output_usd_per_million_tokens": 19.0,
        "cached_input_usd_per_million_tokens": None,
        "source_url": "https://example.invalid",
        "as_of": "2026-08-28",
        "note": "test",
    }], destination)
    monkeypatch.setattr(settings, "ai_cost_pricing_cache_path", str(destination))

    row = next(item for item in pricing_table() if item["model_id"] == "gpt-5.6-sol")
    assert row["label"] == "Updated GPT"
    assert row["input_usd_per_million_tokens"] == 9.0
    assert row["source_url"] == "https://example.invalid"
