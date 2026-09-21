import json
from pathlib import Path


REPORT = json.loads(
    (Path(__file__).parents[1] / "docs/ai/nl-before-after-benchmark.json").read_text(
        encoding="utf-8"
    )
)


def test_before_after_benchmark_covers_accuracy_latency_tokens_and_cost():
    assert REPORT["schema_version"] == "nl-before-after-v1"
    assert REPORT["case_count"] == 100
    assert REPORT["samples_per_case"] == 25
    assert "parser_and_normalizer_only" in REPORT["scope"]
    for side in (REPORT["old"], REPORT["new"]):
        assert side["samples"] == 2500
        assert 0 <= side["intent_accuracy"] <= 1
        assert 0 <= side["language_accuracy"] <= 1
        assert side["latency_ms_p50"] >= 0
        assert side["latency_ms_p95"] >= side["latency_ms_p50"]
        assert side["input_tokens_estimated"] > 0
        assert side["output_tokens_estimated"] > 0
        assert side["reference_cost_usd"] >= 0
    assert REPORT["new"]["intent_accuracy"] >= REPORT["old"]["intent_accuracy"]
    assert REPORT["new"]["entity_accuracy"] >= REPORT["old"]["entity_accuracy"]
