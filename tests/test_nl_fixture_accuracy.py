from pathlib import Path

import pytest
import yaml

from shared.natural_language.router import route_natural_language


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nl_queries_vi.yaml"
CASES = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_vietnamese_baseline_routes_without_side_effects(case):
    result = route_natural_language(case["text"], cluster_id="fixture-cluster")

    assert result.intent == case["intent"]
    assert result.language == case["language"]
    assert result.needs_clarification is case["needs_clarification"]
    if "resource_type" in case:
        assert result.resource_type == case["resource_type"]
    if "resource_ids" in case:
        assert result.resource_ids == tuple(case["resource_ids"])
    if "filters" in case:
        for key, value in case["filters"].items():
            assert result.filters[key] == value
    if "time_range_seconds" in case:
        assert result.time_range.duration_seconds == case["time_range_seconds"]
    assert result.mode == "read_only"
    assert result.decision_reason
