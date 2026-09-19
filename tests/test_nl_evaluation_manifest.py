import json
from pathlib import Path

import yaml

from shared.natural_language import plan_query, route_natural_language


ROOT = Path(__file__).parents[1]
MANIFEST = json.loads(
    (ROOT / "docs/ai/nl-evaluation-manifest.json").read_text(encoding="utf-8")
)
FIXTURE = yaml.safe_load(
    (ROOT / "tests/fixtures/nl_queries_vi.yaml").read_text(encoding="utf-8")
)


def test_evaluation_manifest_has_one_complete_contract_per_fixture_case():
    assert MANIFEST["schema_version"] == "nl-evaluation-v1"
    assert MANIFEST["case_count"] == 100
    assert len(MANIFEST["cases"]) == len(FIXTURE) == 100
    assert {row["id"] for row in MANIFEST["cases"]} == {case["id"] for case in FIXTURE}
    for row in MANIFEST["cases"]:
        expected = row["expected"]
        assert expected["cluster_id"] == "fixture-cluster"
        assert expected["safety"] in {"read_only", "clarification_required"}
        assert set(expected["answer_facts"]) >= {
            "cluster_id", "intent", "freshness", "evidence_refs",
        }
        assert isinstance(expected["tools"], list)
        assert set(expected["entities"]) == {
            "resource_type", "resource_ids", "filters", "time_range_seconds",
        }


def test_evaluation_manifest_matches_current_router_and_query_plan():
    for row in MANIFEST["cases"]:
        expected = row["expected"]
        intent = route_natural_language(row["text"], cluster_id="fixture-cluster")
        assert intent.intent == expected["intent"]
        assert intent.language == expected["language"]
        assert intent.cluster_id == expected["cluster_id"]
        assert intent.resource_type == expected["entities"]["resource_type"]
        assert list(intent.resource_ids) == expected["entities"]["resource_ids"]
        assert dict(intent.filters) == expected["entities"]["filters"]
        assert intent.time_range.duration_seconds == expected["entities"]["time_range_seconds"]
        plan = plan_query(intent)
        assert [call.tool_name for call in plan.calls] == expected["tools"]
